"""The credential broker and reveal enforcement are on by default.

Both switches default on, and both follow the boundary they need when a config
does not name them: without a sandbox and a network proxy, a placeholder has
nowhere to be substituted and a value has nothing to be contained by, so a
config that turns either off and says nothing about the broker gets it off
rather than a broker that breaks git, the forge CLIs and every vault read.
"""

from __future__ import annotations

import sys

import pytest

from istota.config import CredentialBrokerConfig, config_from_document, load_config
from istota.config_mapper import collect_problems
from istota.lib.toml_write import dumps
from istota.setup_wizard import ContainerAnswers, container_config_document


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    # The default follows the platform; every case but one is about Linux.
    monkeypatch.setattr(sys, "platform", "linux")


def _load(tmp_path, document: dict):
    p = tmp_path / "config.toml"
    p.write_text(dumps(document))
    with collect_problems() as problems:
        config = load_config(p)
    broker_problems = [
        ".".join(p["path"]) for p in problems if p["path"][:2] == ("security", "credential_broker")
    ]
    return config.security.credential_broker, broker_problems


def test_the_dataclass_defaults_are_on():
    broker = CredentialBrokerConfig()
    assert broker.enabled is True
    assert broker.enforce_reveal is True
    assert broker.reveal_enforced is True


def test_an_empty_config_runs_the_broker_and_enforces_reveal(tmp_path):
    broker, problems = _load(tmp_path, {})
    assert (broker.enabled, broker.enforce_reveal) == (True, True)
    assert problems == []


def test_no_sandbox_and_no_broker_key_turns_the_broker_off(tmp_path):
    broker, problems = _load(tmp_path, {"security": {"sandbox_enabled": False}})
    assert (broker.enabled, broker.enforce_reveal) == (False, False)
    assert problems == []


def test_off_linux_the_default_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    broker, problems = _load(tmp_path, {})
    assert (broker.enabled, broker.enforce_reveal) == (False, False)
    assert problems == []


def test_off_linux_an_explicit_enable_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    broker, _ = _load(tmp_path, {"security": {"credential_broker": {"enabled": True}}})
    assert (broker.enabled, broker.enforce_reveal) == (True, True)


def test_no_network_proxy_and_no_broker_key_turns_the_broker_off(tmp_path):
    broker, problems = _load(tmp_path, {"security": {"network": {"enabled": False}}})
    assert (broker.enabled, broker.enforce_reveal) == (False, False)
    assert problems == []


def test_an_explicit_enable_without_a_sandbox_is_kept_and_warned(tmp_path):
    broker, problems = _load(tmp_path, {"security": {
        "sandbox_enabled": False,
        "skill_proxy_enabled": False,
        "credential_broker": {"enabled": True},
    }})
    assert broker.enabled is True
    assert problems == ["security.credential_broker.enabled"]


def test_turning_the_broker_off_takes_enforcement_with_it_quietly(tmp_path):
    broker, problems = _load(tmp_path, {"security": {"credential_broker": {"enabled": False}}})
    assert (broker.enabled, broker.enforce_reveal) == (False, False)
    assert problems == []


def test_an_explicit_enforce_without_the_broker_still_warns(tmp_path):
    broker, problems = _load(tmp_path, {"security": {"credential_broker": {
        "enabled": False, "enforce_reveal": True,
    }}})
    assert broker.enforce_reveal is True
    assert broker.reveal_enforced is False
    assert problems == ["security.credential_broker.enforce_reveal"]


def test_enforcement_can_be_turned_off_alone(tmp_path):
    broker, problems = _load(tmp_path, {"security": {"credential_broker": {"enforce_reveal": False}}})
    assert (broker.enabled, broker.enforce_reveal) == (True, False)
    assert problems == []


def test_the_stack_setup_config_runs_the_broker():
    document = container_config_document(ContainerAnswers(user_id="alice"), inline_credentials=True)
    broker = config_from_document(document).security.credential_broker
    assert (broker.enabled, broker.enforce_reveal) == (True, True)


def test_a_config_with_the_sandbox_off_does_not(tmp_path):
    """What the removed standalone wizard wrote, and what `istota serve` on a
    checkout runs: no sandbox and no network proxy, so nowhere to substitute."""
    p = tmp_path / "config.toml"
    p.write_text(
        '[web]\nauth = "none"\n'
        "[security]\nsandbox_enabled = false\nskill_proxy_enabled = false\n"
        "[security.network]\nenabled = false\n"
    )
    broker = load_config(p).security.credential_broker
    assert (broker.enabled, broker.enforce_reveal) == (False, False)
