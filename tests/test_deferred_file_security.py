"""Deferred files stay confined across untrusted filesystem changes."""

import json
import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.skills import email
from istota.sandbox.skill_proxy import SkillProxy


@pytest.fixture
def deferred(tmp_path, monkeypatch):
    directory = tmp_path / "task-temp"
    directory.mkdir()
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_TASK_ID", "123")
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(directory))
    return directory


def test_email_output_leaf_link(deferred, tmp_path):
    canary = tmp_path / "private.json"
    canary.write_text('{"original": true}')
    (deferred / "task_123_email_output.json").symlink_to(canary)
    email.cmd_output(SimpleNamespace(account=None, body_file=None, body="new body",
                                    html=False, subject=None))
    assert json.loads(canary.read_text()) == {"original": True}
    assert json.loads((deferred / "task_123_email_output.json").read_text())["body"] == "new body"


@pytest.mark.parametrize("attack", ["leaf", "parent"])
def test_email_output_through_real_proxy(deferred, tmp_path, attack):
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / "task_123_email_output.json"
    canary.write_text('{"original": true}')
    if attack == "leaf":
        (deferred / canary.name).symlink_to(canary)
    else:
        deferred.rmdir()
        deferred.symlink_to(outside, target_is_directory=True)
    base_env = {name: os.environ[name] for name in (
        "PATH", "ISTOTA_USER_ID", "ISTOTA_TASK_ID", "ISTOTA_DEFERRED_DIR"
    )}
    with tempfile.TemporaryDirectory(prefix="deferred-proxy-", dir="/tmp") as socket_dir:
        address = Path(socket_dir) / "s"
        with SkillProxy(address, {}, base_env, allowed_skills=frozenset({"email"}),
                        trusted_roots=[os.getpid()], task_id=123, user_id="alice"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(15)
                client.connect(str(address))
                client.sendall(json.dumps({"skill": "email", "args": [
                    "output", "--body", "new body"
                ]}).encode() + b"\n")
                result = bytearray()
                while chunk := client.recv(65536):
                    result.extend(chunk)
    response = json.loads(result)
    assert json.loads(canary.read_text()) == {"original": True}
    assert response["returncode"] == (0 if attack == "leaf" else 1)


@pytest.mark.parametrize("producer,suffix", [
    ("kv", "kv_ops"), ("health", "health_ops"),
    ("memory_search", "kg_ops"), ("email", "sent_emails"),
])
def test_append_producers_refuse_link(deferred, tmp_path, producer, suffix):
    import importlib
    module = importlib.import_module(f"istota.skills.{producer}")
    canary = tmp_path / "private.json"
    canary.write_text('[{"private": true}]')
    (deferred / f"task_123_{suffix}.json").symlink_to(canary)
    with pytest.raises(OSError):
        if producer == "email":
            module._write_deferred_sent_email("<test@example.com>", "user@example.com", "subject")
        elif producer == "memory_search":
            module._defer_kg_op({"op": "add_fact"})
        else:
            module._defer_op({"op": "test"})
    assert json.loads(canary.read_text()) == [{"private": True}]


@pytest.mark.parametrize("operation", ["write", "append"])
def test_parent_swap_after_open_stays_in_pinned_directory(deferred, tmp_path, monkeypatch, operation):
    from istota.sandbox import deferred as files
    moved = tmp_path / "original"
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / "task_123_kv_ops.json"
    canary.write_text('[{"private": true}]')
    target = deferred / canary.name
    target.write_text('[{"first": true}]')
    replace = files.replace_at

    def swap_then_replace(fd, name, data, **kwargs):
        deferred.rename(moved)
        deferred.symlink_to(outside, target_is_directory=True)
        replace(fd, name, data, **kwargs)

    monkeypatch.setattr(files, "replace_at", swap_then_replace)
    if operation == "append":
        files.append_json(target, [{"second": True}])
        expected = [{"first": True}, {"second": True}]
    else:
        files.write_json(target, [{"second": True}])
        expected = [{"second": True}]
    assert json.loads(canary.read_text()) == [{"private": True}]
    assert json.loads((moved / target.name).read_text()) == expected


@pytest.mark.parametrize("attack", ["leaf", "parent", "oversized", "directory"])
def test_scheduler_refuses_unsafe_input(deferred, tmp_path, monkeypatch, attack):
    from istota.sandbox import deferred as files
    from istota.scheduler_deferred import _load_deferred_json
    target = deferred / "task_123_kv_ops.json"
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / target.name
    canary.write_text('[{"private": true}]')
    if attack == "leaf":
        target.symlink_to(canary)
    elif attack == "parent":
        deferred.rmdir()
        deferred.symlink_to(outside, target_is_directory=True)
    elif attack == "directory":
        target.mkdir()
    else:
        monkeypatch.setattr(files, "MAX_BYTES", 64)
        target.write_text('["' + 'x' * 64 + '"]')
    assert _load_deferred_json(deferred, 123, "kv_ops") is None
    assert json.loads(canary.read_text()) == [{"private": True}]


def test_scheduler_fifo_does_not_block(deferred):
    import subprocess
    import sys
    target = deferred / "task_123_kv_ops.json"
    os.mkfifo(target)
    code = (
        "from pathlib import Path; "
        "from istota.scheduler_deferred import _load_deferred_json; "
        f"assert _load_deferred_json(Path({str(deferred)!r}), 123, 'kv_ops') is None"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert not target.exists()


def test_reader_uses_open_descriptor(deferred, tmp_path, monkeypatch):
    from istota.sandbox import deferred as files
    target = deferred / "task_123_kv_ops.json"
    target.write_text('[{"safe": true}]')
    canary = tmp_path / "private.json"
    canary.write_text('[{"private": true}]')
    open_regular = files.open_regular_at

    def swap_after_open(fd, name):
        source = open_regular(fd, name)
        target.unlink()
        target.symlink_to(canary)
        return source

    monkeypatch.setattr(files, "open_regular_at", swap_after_open)
    assert files.read_json(target) == [{"safe": True}]


def test_retry_cleanup_refuses_parent_link(deferred, tmp_path):
    from istota.scheduler_deferred import _purge_deferred_files_for_retry
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / "task_123_kv_ops.json"
    canary.write_text("[]")
    deferred.rmdir()
    deferred.symlink_to(outside, target_is_directory=True)
    _purge_deferred_files_for_retry(SimpleNamespace(id=123), deferred)
    assert canary.read_text() == "[]"


def test_appending_preserves_ops_and_bounds_output(deferred, monkeypatch):
    from istota.sandbox import deferred as files
    path = deferred / "task_123_kv_ops.json"
    files.append_json(path, [{"first": True}])
    files.append_json(path, [{"second": True}])
    assert files.read_json(path) == [{"first": True}, {"second": True}]
    original = path.read_bytes()
    monkeypatch.setattr(files, "MAX_BYTES", len(original))
    with pytest.raises(ValueError, match="size limit"):
        files.append_json(path, [{"third": True}])
    assert path.read_bytes() == original


@pytest.mark.parametrize("payload", ["null", "{}", '[[[[[[[[[[[[[[[[[[[['])
def test_malformed_ops_are_removed(deferred, payload):
    from istota.scheduler_deferred import _load_deferred_json
    path = deferred / "task_123_kv_ops.json"
    path.write_text(payload)
    assert _load_deferred_json(deferred, 123, "kv_ops") is None
    assert not path.exists()


@pytest.mark.parametrize("attack", ["leaf", "parent", "oversized"])
def test_email_loader_refuses_unsafe_output(deferred, tmp_path, monkeypatch, attack):
    from istota.config import Config
    from istota.db import Task
    from istota.sandbox import deferred as files
    from istota.transport.email.outbound import _load_deferred_email_output
    monkeypatch.setattr("istota.executor.task_deferred_dir", lambda *_: deferred)
    task = Task(id=123, status="running", source_type="email", user_id="alice", prompt="p")
    target = deferred / "task_123_email_output.json"
    outside = tmp_path / "private"
    outside.mkdir()
    canary = outside / target.name
    payload = {"body": "private", "format": "plain"}
    canary.write_text(json.dumps(payload))
    if attack == "leaf":
        target.symlink_to(canary)
    elif attack == "parent":
        deferred.rmdir()
        deferred.symlink_to(outside, target_is_directory=True)
    else:
        target.write_text(json.dumps(payload))
        monkeypatch.setattr(files, "MAX_BYTES", 8)
    assert _load_deferred_email_output(Config(), task) is None
    assert json.loads(canary.read_text()) == payload


def test_invalid_directory_components_never_use_cwd(deferred, tmp_path, monkeypatch):
    from istota.sandbox import deferred as files
    monkeypatch.chdir(tmp_path)
    canary = tmp_path / "task_123_kv_ops.json"
    canary.write_text('[{"private": true}]')
    target = deferred / ".." / "task_123_kv_ops.json"
    with pytest.raises(OSError):
        files.read_json(target)
    assert json.loads(canary.read_text()) == [{"private": True}]
