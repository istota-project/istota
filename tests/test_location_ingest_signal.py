"""The cross-process signal that the ingest token map has gone stale.

The web app provisions a token; the receiver holds the token map in
memory. They are different processes, so without a signal a freshly
generated token 403s until the receiver restarts — which reads as a bad
token rather than as a stale cache. See
:mod:`istota.location.ingest_signal`.
"""

from __future__ import annotations

import pytest


_needs_fastapi = pytest.mark.skipif(
    pytest.importorskip("fastapi", reason="fastapi not installed") is None,
    reason="fastapi not installed",
)


class TestIngestReloadSignal:
    def test_stamp_is_zero_before_any_signal(self, tmp_path):
        from istota.location import ingest_signal

        assert ingest_signal.reload_stamp(tmp_path / "istota.db") == 0.0

    def test_signal_advances_the_stamp(self, tmp_path):
        from istota.location import ingest_signal

        db_path = tmp_path / "istota.db"
        ingest_signal.signal_reload(db_path)
        first = ingest_signal.reload_stamp(db_path)
        assert first > 0.0

        ingest_signal.signal_reload(db_path)
        assert ingest_signal.reload_stamp(db_path) >= first

    def test_signal_survives_a_missing_directory(self, tmp_path):
        """Never raise into the caller — a failed signal degrades to the
        pre-existing behaviour (reload on SIGHUP or restart), which is a
        delay, not a data loss."""
        from istota.location import ingest_signal

        ingest_signal.signal_reload(tmp_path / "nope" / "deeper" / "istota.db")
        assert ingest_signal.reload_stamp(
            tmp_path / "nope" / "deeper" / "istota.db"
        ) > 0.0


@_needs_fastapi
class TestReceiverReloadsOnSignal:
    def _reset(self, wr, cfg):
        wr._config = cfg
        wr._sentinel_stamp = 0.0

    def test_changed_sentinel_triggers_one_reload(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        from istota.config import Config
        from istota.location import ingest_signal

        cfg = Config(db_path=tmp_path / "istota.db")
        self._reset(wr, cfg)
        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))

        ingest_signal.signal_reload(cfg.db_path)
        wr._maybe_reload_for_signal()
        wr._maybe_reload_for_signal()

        assert len(calls) == 1, "the stamp was not claimed, so it reloaded twice"

    def test_no_sentinel_means_no_reload(self, tmp_path, monkeypatch):
        from istota import webhook_receiver as wr
        from istota.config import Config

        cfg = Config(db_path=tmp_path / "istota.db")
        self._reset(wr, cfg)
        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))

        wr._maybe_reload_for_signal()

        assert calls == []

    def test_unloaded_config_is_a_no_op(self, monkeypatch):
        from istota import webhook_receiver as wr

        wr._config = None
        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))
        wr._maybe_reload_for_signal()
        assert calls == []
