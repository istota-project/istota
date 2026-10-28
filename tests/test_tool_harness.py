"""The cwd rule (`request_tool_workspace`), for both brains.

The tool harness itself is tested in `tests/test_tool_harness_direct.py`;
the relay this file used to hold went with #862's Stage 5.
"""

import os

from istota.brain._tool_harness import request_tool_workspace
from istota.brain._types import BrainRequest, HarnessSpec


def _req(tmp_path, **kw):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    root = tmp_path / "harness-root"
    root.mkdir(exist_ok=True)
    base = dict(
        prompt="",
        allowed_tools=["Read", "Write", "Edit", "Grep", "Glob", "Bash"],
        cwd=workspace,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout_seconds=60,
        task_id=7,
        attempt=1,
        harness=HarnessSpec(cli_env={}, harness_root=root),
    )
    base.update(kw)
    return BrainRequest(**base)


# ---- The cwd rule, on both brains --------------------------------------------


def _roots_req(tmp_path, cwd):
    scratch = tmp_path / "scratch"
    workspace = tmp_path / "workspace"
    for path in (scratch, workspace, workspace / "sub"):
        path.mkdir(parents=True, exist_ok=True)
    return _req(
        tmp_path,
        cwd=cwd,
        fs_read_roots=[scratch, workspace],
        fs_write_roots=[scratch, workspace],
    )


def test_the_cwd_is_the_requests_when_it_lies_in_a_write_root(tmp_path):
    from istota.brain.native import NativeBrain
    from istota.config import NativeBrainConfig

    req = _roots_req(tmp_path, tmp_path / "workspace" / "sub")
    assert request_tool_workspace(req)[0] == tmp_path / "workspace" / "sub"
    native = NativeBrain(NativeBrainConfig(model="m"), provider=object())
    assert native._hello_payload(req)["cwd"] == str(tmp_path / "workspace" / "sub")


def test_the_cwd_falls_back_to_the_first_write_root_outside_them(tmp_path):
    from istota.brain.native import NativeBrain
    from istota.config import NativeBrainConfig

    req = _roots_req(tmp_path, tmp_path)
    assert request_tool_workspace(req)[0] == tmp_path / "scratch"
    # A spelling that only looks inside is outside.
    sneaky = _roots_req(tmp_path, tmp_path / "workspace" / ".." / "elsewhere")
    assert request_tool_workspace(sneaky)[0] == tmp_path / "scratch"
    native = NativeBrain(NativeBrainConfig(model="m"), provider=object())
    assert native._hello_payload(req)["cwd"] == str(tmp_path / "scratch")
