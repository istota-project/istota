"""Legacy profile parking uses real files and never supplies a user's jar."""
import ast
import logging
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from tests import test_browser_pool
from tests.support.drift import source_of

runtime = test_browser_pool.runtime


def test_parks_all_legacy_entries_and_preserves_reserved(runtime, tmp_path, caplog):
    _, chrome, _, _ = runtime
    for name in ("Default", "ssl", "users/bob"):
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / "sentinel").write_text(name)
    (tmp_path / "Local State").write_text("old state")
    (tmp_path / "SingletonLock").symlink_to("absent-lock-target")
    with caplog.at_level(logging.WARNING):
        chrome.migrate_legacy_profile(tmp_path)
    parked = tmp_path / "legacy-profile"
    assert (parked / "Default/sentinel").read_text() == "Default"
    assert (parked / "Local State").read_text() == "old state"
    assert (parked / "SingletonLock").is_symlink()
    assert (tmp_path / "ssl/sentinel").read_text() == "ssl"
    assert (tmp_path / "users/bob/sentinel").read_text() == "users/bob"
    assert str(parked) in caplog.text
    assert "no user inherits it" in caplog.text
    with mock.patch.object(chrome.os, "rename") as rename:
        chrome.migrate_legacy_profile(tmp_path)
    rename.assert_not_called()


def test_fresh_root_is_untouched(runtime, tmp_path):
    _, chrome, _, _ = runtime
    (tmp_path / "ssl").mkdir()
    chrome.migrate_legacy_profile(tmp_path)
    assert list(tmp_path.iterdir()) == [tmp_path / "ssl"]
    chrome.migrate_legacy_profile(tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_parked_profile_never_reaches_first_user(runtime, tmp_path):
    pool, chrome, _, _ = runtime
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default/Cookies").write_text("legacy session")
    chrome.migrate_legacy_profile(tmp_path)
    alice = pool.acquire("alice")
    assert Path(alice.profile_dir) == tmp_path / "users/alice"
    assert list(Path(alice.profile_dir).iterdir()) == []
    assert (tmp_path / "legacy-profile/Default/Cookies").read_text() == "legacy session"


def test_interrupted_park_resumes_after_detection_entries_moved(runtime, tmp_path, monkeypatch):
    _, chrome, _, _ = runtime
    (tmp_path / "Default").mkdir()
    (tmp_path / "Local State").write_text("state")
    (tmp_path / "remaining").write_text("remainder")
    (tmp_path / "z-last").write_text("later entry")
    rename = chrome.os.rename

    def interrupted(src, dst):
        if Path(src).name == "remaining":
            raise OSError("interrupted move")
        rename(src, dst)

    monkeypatch.setattr(chrome.os, "rename", interrupted)
    chrome.migrate_legacy_profile(tmp_path)
    assert (tmp_path / "legacy-profile/z-last").read_text() == "later entry"
    assert not (tmp_path / "Default").exists()
    assert not (tmp_path / "Local State").exists()
    monkeypatch.setattr(chrome.os, "rename", rename)
    chrome.migrate_legacy_profile(tmp_path)
    assert (tmp_path / "legacy-profile/remaining").read_text() == "remainder"


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_collision_preserves_both_copies(runtime, tmp_path, kind, caplog):
    _, chrome, _, _ = runtime
    (tmp_path / "Default").mkdir()
    parked = tmp_path / "legacy-profile"
    parked.mkdir()
    (tmp_path / "collision").write_text("source")
    target = parked / "collision"
    if kind == "file":
        target.write_text("destination")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("absent")
    with caplog.at_level(logging.WARNING):
        chrome.migrate_legacy_profile(tmp_path)
    assert "leaving it for retry" in caplog.text
    assert (tmp_path / "collision").read_text() == "source"
    assert (parked / "Default").is_dir()
    if kind == "file":
        assert target.read_text() == "destination"
    elif kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_symlink()


def test_park_destination_cannot_be_a_symlink(runtime, tmp_path):
    _, chrome, _, _ = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "legacy-profile").symlink_to(outside, target_is_directory=True)
    (tmp_path / "Default").mkdir()
    with pytest.raises(OSError):
        chrome.migrate_legacy_profile(tmp_path)
    assert (tmp_path / "Default").is_dir()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fails", [False, True])
def test_boot_parks_before_starting_any_service(runtime, fails, monkeypatch):
    _, chrome, _, _ = runtime
    # Execute the actual startup block without importing Flask or starting servers.
    module = types.ModuleType("browser_boot_source")
    module.__file__ = str(Path(__file__).resolve().parent.parent / "docker/browser/browse_api.py")
    tree = ast.parse(source_of(module))
    startup = tree.body[-1]
    assert isinstance(startup, ast.If)
    calls = mock.Mock()
    calls.make_server.return_value = ("server", "pending")
    monkeypatch.setitem(sys.modules, "browser_server", types.SimpleNamespace(
        make_browser_server=calls.make_server, serve_browser_requests=calls.serve,
    ))
    calls.chrome.PROFILE_ROOT = chrome.PROFILE_ROOT
    if fails:
        calls.chrome.migrate_legacy_profile.side_effect = OSError("park failed")
    namespace = {"__name__": "__main__", "chrome": calls.chrome,
                 "signal": mock.Mock(), "_exit_on_sigterm": mock.Mock(),
                 "threading": calls.threading, "_resource_monitor": mock.Mock(),
                 "_start_liveness_server": calls.liveness,
                 "_start_browse_watchdog": calls.watchdog, "app": calls.app}
    code = compile(ast.Module(body=[startup], type_ignores=[]), "startup", "exec")
    if fails:
        with pytest.raises(OSError, match="park failed"):
            exec(code, namespace)
        assert calls.mock_calls == [mock.call.chrome.migrate_legacy_profile(chrome.PROFILE_ROOT)]
    else:
        exec(code, namespace)
        assert calls.mock_calls[0] == mock.call.chrome.migrate_legacy_profile(chrome.PROFILE_ROOT)
        calls.make_server.assert_called_once_with(calls.app)
        calls.serve.assert_called_once_with("server", "pending")
