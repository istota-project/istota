"""Tests for publish_shared_kv (admin-shared-briefing-blocks Stage 4)."""

import json

from istota import db, scheduler
from istota.config import Config, UserConfig


def _config(tmp_path, admins=("stefan",)) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"stefan": UserConfig(timezone="UTC")},
        admin_users=set(admins),
    )


def _make_job(conn, user_id, *, publish, trusted=False, name="pub"):
    conn.execute(
        """INSERT INTO scheduled_jobs
           (user_id, name, cron_expression, prompt, enabled,
            publish_shared_kv, publish_shared_kv_trusted)
           VALUES (?, ?, ?, ?, 1, ?, ?)""",
        (user_id, name, "0 7 * * *", "do stuff", publish, 1 if trusted else 0),
    )
    row = conn.execute(
        "SELECT id FROM scheduled_jobs WHERE user_id=? AND name=?", (user_id, name),
    ).fetchone()
    return db.get_scheduled_job(conn, row[0])


class TestParseTarget:
    def test_bare_key_maps_to_shared_block_ns(self):
        assert scheduler._parse_shared_kv_target("film-digest") == (
            "briefing_shared_blocks", "film-digest",
        )

    def test_ns_slash_key(self):
        assert scheduler._parse_shared_kv_target("custom/thing") == ("custom", "thing")


class TestPublish:
    def test_authorized_write(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)

        class _T:
            user_id = "stefan"
        with db.get_db(cfg.db_path) as conn:
            job = _make_job(conn, "stefan", publish="film-digest", trusted=False)
            ok = scheduler._publish_result_to_shared_kv(conn, cfg, _T(), job, "# Digest\nbody")
            assert ok is True
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "film-digest")
        assert json.loads(row["value"]) == {"text": "# Digest\nbody", "trusted": False}
        assert row["written_by"] == "stefan"

    def test_trusted_flag_written(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)

        class _T:
            user_id = "stefan"
        with db.get_db(cfg.db_path) as conn:
            job = _make_job(conn, "stefan", publish="markets", trusted=True)
            scheduler._publish_result_to_shared_kv(conn, cfg, _T(), job, "table")
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "markets")
        assert json.loads(row["value"])["trusted"] is True

    def test_ns_key_target(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)

        class _T:
            user_id = "stefan"
        with db.get_db(cfg.db_path) as conn:
            job = _make_job(conn, "stefan", publish="custom/key1")
            scheduler._publish_result_to_shared_kv(conn, cfg, _T(), job, "x")
            assert db.shared_kv_get(conn, "custom", "key1") is not None

    def test_unauthorized_fails_loud(self, tmp_path, monkeypatch):
        # No admin allowlist entry for the task user → not a shared_kv writer.
        cfg = _config(tmp_path, admins=("someoneelse",))
        db.init_db(cfg.db_path)
        alerts = []
        monkeypatch.setattr(
            scheduler, "_send_operator_alert",
            lambda config, user, msg, **k: alerts.append(msg),
        )

        class _T:
            user_id = "stefan"
        with db.get_db(cfg.db_path) as conn:
            job = _make_job(conn, "stefan", publish="film-digest")
            ok = scheduler._publish_result_to_shared_kv(conn, cfg, _T(), job, "body")
            assert ok is False
            # Nothing written.
            assert db.shared_kv_get(conn, "briefing_shared_blocks", "film-digest") is None
            # Failure recorded.
            fresh = db.get_scheduled_job(conn, job.id)
            assert fresh.consecutive_failures == 1
        assert alerts and "non-writer" in alerts[0]

    def test_empty_result_skips(self, tmp_path):
        cfg = _config(tmp_path)
        db.init_db(cfg.db_path)

        class _T:
            user_id = "stefan"
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(
                conn, "briefing_shared_blocks", "film-digest",
                json.dumps({"text": "old"}), "stefan",
            )
            job = _make_job(conn, "stefan", publish="film-digest")
            ok = scheduler._publish_result_to_shared_kv(conn, cfg, _T(), job, "   ")
            assert ok is True  # clean skip, not a failure
            row = db.shared_kv_get(conn, "briefing_shared_blocks", "film-digest")
        assert json.loads(row["value"]) == {"text": "old"}  # last-known-good
