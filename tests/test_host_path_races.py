"""Host skills must not follow a path replaced after argument validation."""

import json
import os
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.sandbox.host_paths import (
    open_resolved, resolve_host_path, skill_file_env, write_resolved,
)
from istota.skills._cli import parse_and_resolve
from istota.skills import email


@pytest.fixture
def roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(allowed))
    monkeypatch.setenv("ISTOTA_TASK_ID", "123")
    monkeypatch.delenv("ISTOTA_WORKSPACE_PATH", raising=False)
    monkeypatch.delenv("NEXTCLOUD_MOUNT_PATH", raising=False)
    return allowed, outside


@pytest.mark.parametrize("swap_parent", [False, True])
def test_email_body_swap_after_parse_is_refused(roots, swap_parent):
    allowed, outside = roots
    parent = allowed / "inputs"
    parent.mkdir()
    body = parent / "body.txt"
    body.write_text("approved body")
    (outside / "body.txt").write_text("outside canary")
    args = parse_and_resolve(email.build_parser(), ["output", "--body-file", str(body)])
    if swap_parent:
        parent.rename(allowed / "original-inputs")
        parent.symlink_to(outside, target_is_directory=True)
    else:
        body.unlink()
        body.symlink_to(outside / "body.txt")

    with pytest.raises(OSError):
        email.cmd_output(args)
    assert not (allowed / "task_123_email_output.json").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_write_refuses_parent_replaced_after_validation(roots, existing):
    allowed, outside = roots
    parent = allowed / "exports"
    parent.mkdir()
    target = parent / "report.txt"
    if existing:
        target.write_text("old report")
        (outside / "report.txt").write_text("outside canary")
    resolved, error = resolve_host_path(target, writable=True, operation="export")
    assert error is None
    parent.rename(allowed / "original-exports")
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        write_resolved(resolved, b"replacement")
    if existing:
        assert (outside / "report.txt").read_text() == "outside canary"
    else:
        assert not (outside / "report.txt").exists()


def test_email_output_accepts_regular_body(roots):
    allowed, _ = roots
    body = allowed / "body.txt"
    body.write_text("approved body")
    args = parse_and_resolve(email.build_parser(), ["output", "--body-file", str(body)])
    result = email.cmd_output(args)
    assert json.loads(Path(result["file"]).read_text())["body"] == "approved body"


def test_attachment_loading_refuses_replaced_parent(roots):
    allowed, outside = roots
    parent = allowed / "inputs"
    parent.mkdir()
    source = parent / "report.txt"
    source.write_text("approved attachment")
    (outside / "report.txt").write_text("outside canary")
    resolved, error = resolve_host_path(source, writable=False, operation="attach")
    assert error is None
    parent.rename(allowed / "original-inputs")
    parent.symlink_to(outside, target_is_directory=True)
    message = EmailMessage()
    with pytest.raises(OSError):
        email._attach_files(message, [str(resolved)])
    assert not list(message.iter_attachments())


def test_an_open_file_stays_pinned_after_parent_replacement(roots):
    allowed, outside = roots
    parent = allowed / "inputs"
    parent.mkdir()
    source = parent / "report.txt"
    source.write_bytes(b"approved bytes")
    (outside / "report.txt").write_bytes(b"outside canary")
    with os.fdopen(open_resolved(source), "rb") as handle:
        parent.rename(allowed / "original-inputs")
        parent.symlink_to(outside, target_is_directory=True)
        assert handle.read() == b"approved bytes"


def test_ocr_gets_a_private_copy_that_survives_source_replacement(roots, monkeypatch):
    from istota.skills import transcribe

    allowed, outside = roots
    source = allowed / "image.png"
    source.write_bytes(b"approved image")
    private = outside / "image.png"
    private.write_bytes(b"outside canary")
    staged = []

    def consume(path, **kwargs):
        staged.append(Path(path))
        source.unlink()
        source.symlink_to(private)
        assert staged[-1] != source
        assert staged[-1].read_bytes() == b"approved image"
        return {"status": "ok", "text": "decoded"}

    monkeypatch.setattr(transcribe, "ocr_image", consume)
    assert transcribe.cmd_ocr(SimpleNamespace(image_path=str(source), preprocess=False))["status"] == "ok"
    assert not staged[0].exists()


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_read_and_write_refuse_non_regular_objects(roots, kind):
    allowed, _ = roots
    target = allowed / "not-a-file"
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(OSError):
        open_resolved(target)
    with pytest.raises(OSError):
        write_resolved(target, b"replacement")


def test_gws_passes_a_read_only_private_copy(roots, monkeypatch):
    from istota.skills.google_workspace import main
    from tests.support.skill_cli import run_skill_main

    allowed, outside = roots
    source = allowed / "report.txt"
    source.write_text("approved input")
    private = outside / "report.txt"
    private.write_text("outside canary")
    host_files = outside / "host-files"
    host_files.mkdir()
    monkeypatch.setenv("ISTOTA_HOST_FILES_DIR", str(host_files))
    monkeypatch.chdir(allowed)
    staged = []

    def consume(program, argv):
        staged.append(Path(argv[-1]))
        source.unlink()
        source.symlink_to(private)
        assert staged[-1] != source
        assert staged[-1].read_text() == "approved input"
        assert staged[-1].stat().st_mode & 0o222 == 0
        assert Path.cwd().stat().st_mode & 0o222 == 0

    monkeypatch.setattr(os, "execvp", consume)
    run = run_skill_main(main, ["drive", "+upload", str(source)])
    assert run.exit_code == 0
    assert staged
    assert not staged[0].exists()


@pytest.mark.parametrize("fails", [False, True])
def test_skill_parent_reaps_read_only_staging_even_on_failure(tmp_path, fails):
    original = {"ISTOTA_HOST_FILES_DIR": str(tmp_path / "untrusted"), "EXAMPLE": "value"}
    try:
        with skill_file_env(original) as env:
            directory = Path(env["ISTOTA_HOST_FILES_DIR"])
            assert directory != Path(original["ISTOTA_HOST_FILES_DIR"])
            assert env["EXAMPLE"] == "value"
            child = directory / "input"
            child.mkdir()
            (child / "body.txt").write_text("approved input")
            (child / "body.txt").chmod(0o400)
            child.chmod(0o500)
            if fails:
                raise RuntimeError("child failed")
    except RuntimeError:
        assert fails
    assert not directory.exists()
    assert original["ISTOTA_HOST_FILES_DIR"] == str(tmp_path / "untrusted")
