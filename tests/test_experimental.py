"""Tests for the experimental feature flag mechanism."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from istota.config import Config, ExperimentalConfig, load_config
from istota.experimental import (
    KNOWN_FEATURES,
    enabled_features_from_env,
    requires_feature,
)


class TestExperimentalConfig:
    def test_default_empty(self):
        cfg = ExperimentalConfig()
        assert cfg.features == []
        assert cfg.is_enabled("module_health") is False

    def test_is_enabled_match(self):
        cfg = ExperimentalConfig(features=["module_health", "money_tax"])
        assert cfg.is_enabled("module_health") is True
        assert cfg.is_enabled("money_tax") is True
        assert cfg.is_enabled("nope") is False


class TestLoadConfigExperimental:
    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(body)
        return p

    def test_no_section_yields_empty(self, tmp_path):
        path = self._write(tmp_path, "namespace = 'istota'\n")
        cfg = load_config(path)
        assert cfg.experimental.features == []

    def test_populated_section(self, tmp_path):
        path = self._write(tmp_path, (
            "[experimental]\n"
            "features = ['module_health', 'money_tax']\n"
        ))
        cfg = load_config(path)
        assert cfg.experimental.features == ["module_health", "money_tax"]
        assert cfg.experimental.is_enabled("module_health")

    def test_unknown_feature_logs_warning(self, tmp_path, caplog):
        path = self._write(tmp_path, (
            "[experimental]\n"
            "features = ['module_health', 'totally_made_up']\n"
        ))
        with caplog.at_level(logging.WARNING, logger="istota.config"):
            cfg = load_config(path)
        assert "totally_made_up" in caplog.text
        # Known features still load; unknown ones come through but warn
        assert "totally_made_up" in cfg.experimental.features
        assert "module_health" in cfg.experimental.features


class TestEnabledFeaturesFromEnv:
    def test_unset(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        assert enabled_features_from_env() == frozenset()

    def test_empty_string(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "")
        assert enabled_features_from_env() == frozenset()

    def test_csv_parses(self, monkeypatch):
        monkeypatch.setenv(
            "ISTOTA_EXPERIMENTAL_FEATURES", "module_health,money_tax",
        )
        assert enabled_features_from_env() == frozenset(
            {"module_health", "money_tax"},
        )

    def test_whitespace_tolerated(self, monkeypatch):
        monkeypatch.setenv(
            "ISTOTA_EXPERIMENTAL_FEATURES", " module_health , money_tax , ",
        )
        assert enabled_features_from_env() == frozenset(
            {"module_health", "money_tax"},
        )


class TestRequiresFeature:
    """The decorator runs inside a Click command — we exercise it via a
    fake command and ``CliRunner`` so the decorator stack matches real use."""

    def _make_command(self, flag: str) -> click.Command:
        @click.command()
        @requires_feature(flag)
        def cmd():
            print(json.dumps({"status": "ok", "value": 42}))

        return cmd

    def test_off_emits_error_envelope_and_exits_1(self, monkeypatch):
        monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
        runner = CliRunner()
        result = runner.invoke(self._make_command("money_tax"))
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "money_tax" in payload["error"]
        assert "experimental" in payload["error"]

    def test_on_passes_through(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "money_tax")
        runner = CliRunner()
        result = runner.invoke(self._make_command("money_tax"))
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["value"] == 42

    def test_other_flag_doesnt_let_through(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "money_wash_sales")
        runner = CliRunner()
        result = runner.invoke(self._make_command("money_tax"))
        assert result.exit_code == 1


class TestRegistry:
    def test_known_features_includes_module_health(self):
        assert "module_health" in KNOWN_FEATURES
        assert "money_tax" in KNOWN_FEATURES
        assert "money_wash_sales" in KNOWN_FEATURES


class TestIsModuleEnabledExperimental:
    def test_health_off_by_default(self):
        cfg = Config()
        assert cfg.is_module_enabled("alice", "health") is False

    def test_health_on_when_flag_enabled(self):
        cfg = Config(experimental=ExperimentalConfig(features=["module_health"]))
        assert cfg.is_module_enabled("alice", "health") is True

    def test_non_experimental_module_unaffected(self):
        cfg = Config()
        # Standard modules stay enabled with no flag set
        assert cfg.is_module_enabled("alice", "feeds") is True

    def test_unknown_module_returns_false(self):
        cfg = Config(experimental=ExperimentalConfig(features=["module_health"]))
        assert cfg.is_module_enabled("alice", "definitely_not_a_module") is False
