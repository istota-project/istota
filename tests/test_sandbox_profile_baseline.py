"""The CLAUDE profile still emits exactly what `build_bwrap_cmd` emitted before
the profile split.

`tests/test_sandbox.py::TestSandboxProfiles` pins the *difference* between the
two profiles: delete the Claude operations from the CLAUDE plan and you get the
NATIVE plan. That says the split is internally consistent. It does not say the
CLAUDE plan is what the two Claude brains were getting yesterday — both argvs
could have drifted together and every assertion there would still hold, on the
one code path that runs the majority of tasks on the reference deployment.

So this file compares against the real thing: the pre-split `build_bwrap_cmd`,
loaded out of git at the commit named below and run in the same process, on the
same host, against the same `Config`. Every host-dependent input (which of
`/bin` and `/lib` are symlinks, which `/etc` files exist, what the bwrap feature
probes answer, where the venv is) is therefore identical for both, which is what
a committed golden of absolute paths could not have been.

Why a pinned sha and not a golden file: the argv is nothing but absolute paths,
several of them decided by the host rather than by the code. A golden captured
on one machine matches on that machine. A differential run matches everywhere.

**If this file stops loading its baseline it must not quietly pass.** It skips
only when git itself cannot answer — a source tree with no `.git`, which is how
the test image is built — and says so. A baseline that loads but whose
`build_bwrap_cmd` already takes a `profile` means the sha is wrong and the
comparison is the new code against itself; that is a failure, not a skip.
"""

import subprocess
import sys
import types
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, SecurityConfig
from istota.executor import SandboxProfile, build_bwrap_cmd

#: The commit immediately before the `SandboxProfile` split, i.e. the last one
#: whose `build_bwrap_cmd` emitted the Claude runtime block unconditionally.
BASELINE_SHA = "8ea692e34958ef2cb4a3cddb8586280463921751"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_baseline():
    """The pre-split `executor` module, exec'd as a sibling of the real one.

    ``__package__`` is what makes its relative imports (`from . import db`)
    resolve, and ``__file__`` is what makes `_source_and_venv_paths` derive the
    same venv and source tree the live module does.
    """
    try:
        blob = subprocess.run(
            ["git", "show", f"{BASELINE_SHA}:src/istota/executor.py"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git cannot be run here, so the baseline is unreachable: {exc}")
    if blob.returncode != 0:  # pragma: no cover
        pytest.skip(
            "the baseline blob is not in this checkout "
            f"({BASELINE_SHA[:12]}): {blob.stderr.strip()[:200]}"
        )

    name = "istota._executor_pre_profile_split"
    mod = types.ModuleType(name)
    mod.__file__ = str(_REPO_ROOT / "src" / "istota" / "executor.py")
    mod.__package__ = "istota"
    sys.modules[name] = mod
    try:
        exec(
            compile(blob.stdout, f"<{BASELINE_SHA[:12]}:executor.py>", "exec"),
            mod.__dict__,
        )
    finally:
        sys.modules.pop(name, None)

    assert "profile" not in signature(mod.build_bwrap_cmd).parameters, (
        f"{BASELINE_SHA[:12]} already has the profile split — this file would be "
        "comparing the new code against itself. Point BASELINE_SHA at the commit "
        "before it."
    )
    return mod


@pytest.fixture(scope="module")
def baseline():
    return _load_baseline()


#: `(remount_ro, disable_userns, requires_unshare_user)` — the three bwrap
#: feature probes that change the argv. Both the deployment shape and the
#: container shape, plus the as-root-with-a-non-setuid-bwrap one.
_BWRAP_FEATURE_SHAPES = [
    (True, True, False),
    (False, False, False),
    (False, False, True),
]


@pytest.fixture(params=_BWRAP_FEATURE_SHAPES, ids=lambda s: "-".join(map(str, s)))
def bwrap_features(request, baseline, monkeypatch):
    """Pin the host feature probes to the same answers in both modules.

    Not tidiness. Each of `_bwrap_supports_remount_ro`,
    `_bwrap_supports_disable_userns` and `_bwrap_requires_unshare_user` is
    `lru_cache`d *per module*, and the baseline is a second module — so the two
    caches are filled at different moments in a session. Anything that makes a
    probe answer differently in between (a test that puts a fake `bwrap` on
    PATH, which this suite does) leaves one module believing in `--remount-ro`
    and the other not, and the comparison fails on a host fact rather than on
    the change under test. Observed exactly that way, as a flake that reproduced
    only in a full run.

    Parametrised rather than pinned to one shape, since the flags decide real
    argv and the reproduction claim should hold under each.
    """
    import istota.executor as live

    remount_ro, disable_userns, requires_unshare = request.param
    for mod in (live, baseline):
        monkeypatch.setattr(mod, "_bwrap_supports_remount_ro", lambda: remount_ro)
        monkeypatch.setattr(
            mod, "_bwrap_supports_disable_userns", lambda: disable_userns
        )
        monkeypatch.setattr(
            mod, "_bwrap_requires_unshare_user", lambda: requires_unshare
        )
    return request.param


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """A HOME with a sentinel at every path the Claude runtime block binds.

    Without it the comparison would be strong on a machine with a Claude
    install and vacuous on one without: `_ro_bind` skips a missing source, so
    the block that has to be reproduced would be empty and the equality would
    hold for reasons that have nothing to do with the split.
    """
    home = tmp_path / "home"
    for d in (
        ".local/bin", ".local/share/claude", ".local/state/claude",
        ".claude/projects", ".claude/debug", ".claude/todos",
        ".cache/huggingface",
    ):
        (home / d).mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"token": "sentinel"}')
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


def _layout(tmp_path):
    """A config exercising the parts of the plan that are easy to disturb: a
    Nextcloud mount with a user, a Talk dir and a channel dir, a temp dir with a
    `.developer` subtree, databases to mask, and the developer repos root the
    package cache derives from."""
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Channels" / "room123").mkdir(parents=True)
    (mount / "Talk").mkdir()

    db_file = tmp_path / "data" / "istota.db"
    db_file.parent.mkdir(parents=True)
    db_file.touch()

    repos = tmp_path / "repos"
    (repos / "alice").mkdir(parents=True)

    temp = tmp_path / "temp"
    (temp / "alice" / ".developer").mkdir(parents=True)

    config = Config(
        db_path=db_file,
        temp_dir=temp,
        nextcloud_mount_path=mount,
        skills_dir=tmp_path / "config" / "skills",
        security=SecurityConfig(sandbox_enabled=True),
        developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
    )
    config.admin_users = {"alice"}
    return config, temp / "alice"


def _task(**overrides):
    fields = {
        "id": 1, "prompt": "test", "user_id": "alice", "source_type": "talk",
        "status": "running", "conversation_token": "room123",
    }
    fields.update(overrides)
    return db.Task(**fields)


class TestTheClaudeProfileReproducesThePreSplitArgv:
    """Element for element, in order."""

    @staticmethod
    def _pair(baseline, config, task, user_temp, *, is_admin=False, **kwargs):
        """The same call through both implementations, under one bwrap patch."""
        with patch("istota.executor._bwrap_available", return_value=True), \
             patch.object(baseline, "_bwrap_available", lambda: True):
            before = baseline.build_bwrap_cmd(
                ["claude", "-p", "test"], config, task, is_admin, [], user_temp,
                **kwargs,
            )
            after = build_bwrap_cmd(
                ["claude", "-p", "test"], config, task, is_admin, [], user_temp,
                profile=SandboxProfile.CLAUDE, **kwargs,
            )
        return before, after

    def test_plain_non_admin_task(
        self, baseline, tmp_path, claude_home, bwrap_features,
    ):
        config, user_temp = _layout(tmp_path)
        before, after = self._pair(baseline, config, _task(), user_temp)

        assert after == before
        # Non-vacuous: the Claude block really is in this argv.
        assert str((claude_home / ".claude" / ".credentials.json").resolve()) in after

    def test_admin_task_with_developer_repos_and_a_derived_cache(
        self, baseline, tmp_path, claude_home, bwrap_features,
    ):
        """The ordering-sensitive shape: the cache bind, then the repos bind
        that covers it, then the masks."""
        config, user_temp = _layout(tmp_path)
        before, after = self._pair(
            baseline, config, _task(), user_temp, is_admin=True,
            authorized_skills=frozenset({"developer"}),
        )

        assert after == before
        assert str(
            (Path(config.developer.repos_dir) / "alice" / ".package-caches").resolve()
        ) in after

    def test_with_a_custom_system_prompt_a_proxy_socket_and_the_net_bridge(
        self, baseline, tmp_path, claude_home, bwrap_features,
    ):
        """The three optional binds most likely to be disturbed by a gate placed
        one line off: the system-prompt file (now CLAUDE-only), the skill proxy
        socket, and the `--unshare-net` wrapper."""
        config, user_temp = _layout(tmp_path)
        config.custom_system_prompt = True
        sp = config.skills_dir.parent / "system-prompt.md"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("custom prompt")
        proxy_sock = user_temp / "skill.sock"
        proxy_sock.touch()
        net_sock = user_temp / "net.sock"
        net_sock.touch()

        before, after = self._pair(
            baseline, config, _task(), user_temp,
            proxy_sock=proxy_sock, net_proxy_sock=net_sock,
        )

        assert after == before
        assert str(sp) in after
        assert "--unshare-net" in after

    def test_the_native_profile_only_ever_removes(
        self, baseline, tmp_path, claude_home, bwrap_features,
    ):
        """The other direction, stated against the baseline rather than against
        the new CLAUDE plan: NATIVE introduces nothing that was not already
        emitted before the split.

        A change that hardened one path while adding a *new* mount to the other
        would pass every assertion above.
        """
        config, user_temp = _layout(tmp_path)
        with patch("istota.executor._bwrap_available", return_value=True), \
             patch.object(baseline, "_bwrap_available", lambda: True):
            before = baseline.build_bwrap_cmd(
                ["claude", "-p", "test"], config, _task(), False, [], user_temp,
            )
            native = build_bwrap_cmd(
                ["claude", "-p", "test"], config, _task(), False, [], user_temp,
                profile=SandboxProfile.NATIVE,
            )

        assert set(native) <= set(before)
        assert len(native) < len(before)
