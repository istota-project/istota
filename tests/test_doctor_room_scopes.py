"""`security.room_scope_confinement` (multiplayer Stage 28, ISSUE-576).

A guest's turn withholds every scope at six seams; the filesystem half (the
workspace bind, the per-task temp dir) exists only where a task runs inside
bubblewrap. On the shipped Docker stack, macOS and the standalone install the
other seams still hold, and the host's files are reachable.
"""


from istota import doctor
from istota.config import Config

NAME = "security.room_scope_confinement"


def _check(monkeypatch, *, sandboxed, probe=False):
    monkeypatch.setattr(
        doctor, "_deployment_sandboxing",
        lambda _config, _probe: (sandboxed, "" if sandboxed is not None else "cold memo"),
    )
    return doctor.check_room_scope_confinement(
        Config(), probe,
    )


def test_it_is_registered_as_a_deployment_check():
    assert NAME in dict(doctor.CHECKS)
    assert doctor.CHECK_SCOPES[NAME] == doctor.DEPLOYMENT


def test_a_sandboxed_deployment_confines_withheld_scopes(monkeypatch):
    result = _check(monkeypatch, sandboxed=True)
    assert result.name == NAME and result.status == doctor.OK


def test_an_unsandboxed_deployment_warns_that_files_stay_reachable(monkeypatch):
    result = _check(monkeypatch, sandboxed=False)
    assert result.status == doctor.WARN
    assert "filesystem" in result.detail
    assert result.remedy


def test_an_unestablished_answer_says_so(monkeypatch):
    result = _check(monkeypatch, sandboxed=None)
    assert result.status == doctor.WARN
    assert "cold memo" in result.detail

