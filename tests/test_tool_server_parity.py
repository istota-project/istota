"""Each tool in-process and through the server, against the same fixtures.

Goal 5 of the spec: *tools behave identically either way*. `build_default_tools`
still exists and is what the server runs, so the two paths execute the same
code — but between them now sit a JSON codec, a socket, a process boundary and
a content round trip, and every one of those is somewhere a result can quietly
change shape. A truncation note that lost its newline, an `is_error` flag that
did not cross, an `on_update` chunking that coalesced differently: each would
be invisible to the schema-parity file and visible to the model.

So each case runs the *same arguments* against a local tool and a proxy tool
bound to the same workspace, and compares what came back rather than asserting
on a shape written down here.

The workspace is rebuilt between the two runs where a tool mutates it, because
"identical" means identical from the same starting state.
"""

import asyncio
import shutil

import pytest

from istota.session.tools import ToolEnv, build_default_tools, hello_payload, start_tool_server

pytestmark = pytest.mark.asyncio


def _seed(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("alpha\nbeta\ngamma\n")
    (root / "data.py").write_text("import os\n\n\ndef go():\n    return os.getcwd()\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "deep.txt").write_text("beta again\n")
    (root / "binary.bin").write_bytes(b"\x00\x01\x02binary")
    return root


def _hello(ws, **kw):
    defaults = ToolEnv(cwd=ws)
    args = dict(
        cwd=ws,
        subprocess_env=None,
        read_roots=(ws,),
        write_roots=(ws,),
        write_denied_roots=(),
        deferred_dir=None,
        bash_timeout_seconds=30,
        max_output_bytes=defaults.max_output_bytes,
        max_read_lines=defaults.max_read_lines,
        max_read_bytes=defaults.max_read_bytes,
        bash_spill_full_output=True,
    )
    args.update(kw)
    return hello_payload(**args)


def _local_env(ws):
    return ToolEnv(cwd=ws, read_roots=(ws,), write_roots=(ws,))


def _rendered(result):
    """Everything a caller of a tool can observe, as one comparable value."""
    return (
        [(getattr(b, "type", ""), getattr(b, "text", ""), getattr(b, "data", ""))
         for b in result.content],
        result.is_error,
        result.terminate,
    )


def _normalize(value, local_root, remote_root):
    """Paths are in the results, and the two runs used different directories."""
    blocks, is_error, terminate = value
    a, b = str(local_root), str(remote_root)
    return (
        [(t, text.replace(a, "<WS>").replace(b, "<WS>"), data) for t, text, data in blocks],
        is_error,
        terminate,
    )


CASES = [
    ("Read", {"file_path": "<WS>/notes.txt"}),
    ("Read", {"file_path": "<WS>/notes.txt", "offset": 2, "limit": 1}),
    ("Read", {"file_path": "<WS>/missing.txt"}),
    ("Read", {"file_path": "<WS>/binary.bin"}),
    ("Read", {"file_path": "<WS>/sub"}),
    ("Read", {"file_path": "/etc/hostname"}),  # outside the roots
    ("Write", {"file_path": "<WS>/new.txt", "content": "written\n"}),
    ("Write", {"file_path": "<WS>/notes.txt", "content": "replaced\n"}),
    ("Write", {"file_path": "/tmp/istota-parity-escape.txt", "content": "no"}),
    ("Edit", {"file_path": "<WS>/notes.txt", "old_string": "beta", "new_string": "BETA"}),
    ("Edit", {"file_path": "<WS>/notes.txt", "old_string": "nope", "new_string": "x"}),
    ("Edit", {"file_path": "<WS>/notes.txt",
              "edits": [{"old_string": "alpha", "new_string": "A"},
                        {"old_string": "gamma", "new_string": "G"}]}),
    ("Grep", {"pattern": "beta", "path": "<WS>"}),
    ("Grep", {"pattern": "beta", "path": "<WS>", "output_mode": "content"}),
    ("Grep", {"pattern": "beta", "path": "<WS>", "output_mode": "count"}),
    ("Grep", {"pattern": "b(", "path": "<WS>"}),  # invalid regex
    ("Grep", {"pattern": "os", "path": "<WS>", "glob": "*.py", "output_mode": "content"}),
    ("Glob", {"pattern": "**/*.txt", "path": "<WS>"}),
    ("Glob", {"pattern": "*.nothing", "path": "<WS>"}),
    ("Glob", {"pattern": "*", "path": "/etc"}),  # outside the roots
    ("Bash", {"command": "echo one; echo two"}),
    ("Bash", {"command": "exit 7"}),
    ("Bash", {"command": "false | cat"}),
    ("Bash", {"command": "echo noisy", "exclude_from_context": True}),
    ("Bash", {"command": "printf 'no newline'"}),
]


def _fill(args, root):
    return {k: (v.replace("<WS>", str(root)) if isinstance(v, str) else v)
            for k, v in args.items()}


@pytest.mark.parametrize(
    "tool_name,args",
    CASES,
    ids=[f"{n}-{i}" for i, (n, _) in enumerate(CASES)],
)
async def test_a_tool_answers_the_same_in_process_and_through_the_server(
    tmp_path, tool_name, args,
):
    local_root = _seed(tmp_path / "local")
    remote_root = _seed(tmp_path / "remote")

    local_updates: list[str] = []

    async def _local_update(text):
        local_updates.append(text)

    tool = next(
        t for t in build_default_tools(_local_env(local_root))
        if t.schema.name == tool_name
    )
    local_args = _fill(args, local_root)
    if tool.prepare_arguments is not None:
        local_args = tool.prepare_arguments(local_args)
    local = await tool.execute("c1", local_args, _local_update, None)

    remote_updates: list[str] = []

    async def _remote_update(text):
        remote_updates.append(text)

    server = await start_tool_server(_hello(remote_root))
    try:
        proxy_args = _fill(args, remote_root)
        # The proxy's `prepare_arguments` is the *same object*, so applying it
        # here mirrors exactly what the loop does before either call.
        if tool.prepare_arguments is not None:
            proxy_args = tool.prepare_arguments(proxy_args)
        remote = await server.call(tool_name, "c1", proxy_args, _remote_update, None)
        # Updates are dispatched off the result path, so give them a moment.
        for _ in range(50):
            if remote_updates or not local_updates:
                break
            await asyncio.sleep(0.02)
    finally:
        await server.aclose()

    assert _normalize(_rendered(remote), local_root, remote_root) == _normalize(
        _rendered(local), local_root, remote_root
    )
    # The streamed text, not its chunking: a socket read boundary is not a
    # behaviour difference, and asserting on chunk counts would make this file
    # fail on a fast machine.
    assert "".join(remote_updates) == "".join(local_updates)

    # And the same effect on disk, which is what a comparison of return values
    # alone would miss for Write and Edit.
    assert sorted(p.name for p in remote_root.rglob("*")) == sorted(
        p.name for p in local_root.rglob("*")
    )
    for path in sorted(remote_root.rglob("*")):
        if path.is_file():
            twin = local_root / path.relative_to(remote_root)
            assert path.read_bytes() == twin.read_bytes(), path.name


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
async def test_a_spilled_bash_output_names_a_path_the_read_tool_can_reach(tmp_path):
    """`_SpillWriter` moved into the sandbox with the tool. The path it names
    is inside the namespace, and `Read` reads it back from in there — so the
    two have to agree, which they only do if both run in the same process."""
    ws = _seed(tmp_path / "ws")
    deferred = ws / "deferred"
    deferred.mkdir()
    server = await start_tool_server(
        _hello(ws, deferred_dir=deferred, max_output_bytes=200)
    )
    try:
        result = await server.call(
            "Bash", "c1",
            {"command": "for i in $(seq 1 400); do echo line-$i; done"},
            None, None,
        )
        text = "".join(b.text for b in result.content)
        assert "output truncated" in text
        spilled = text.split("full output: ")[1].rstrip("]\n")
        read_back = await server.call("Read", "c2", {"file_path": spilled}, None, None)
        assert "line-400" in "".join(b.text for b in read_back.content)
    finally:
        await server.aclose()
