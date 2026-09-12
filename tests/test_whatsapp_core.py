"""Stage-one contracts for the WhatsApp Cloud API surface.

The shape mirrors `tests/test_sms_foundation.py`, because the two surfaces are
the same kind of thing: a non-room, ledger-backed, one-send push transport with
an operator-assigned per-user identity. What differs is what identity *is* —
SMS binds one E.164 number on `user_profiles`, WhatsApp binds a Business-Scoped
User ID on a table of its own — and that difference is what most of the
uniqueness and enrollment cases below are about.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota import admin_config_view, db, doctor, user_profiles
from istota.config import (
    Config,
    WhatsAppConfig,
    WhatsAppTemplateConfig,
    load_config,
    whatsapp_config_errors,
    whatsapp_structural_config_errors,
)

REPO = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def _valid_whatsapp_config() -> str:
    return """
[site]
hostname = "assistant.example.com"

[whatsapp]
enabled = true
waba_id = "123456789012345"
phone_number_id = "223456789012345"
business_phone_number = "+15551234567"
access_token = "wa-access-token"
app_secret = "wa-app-secret"
verify_token = "wa-verify-token"
graph_api_version = ""
business_timezone = "America/Los_Angeles"
request_timeout_seconds = 10
billing_policy = "free_guard"
monthly_service_attempt_limit = 900

[whatsapp.proactive_template]
enabled = false
name = ""
language = "en_US"
"""


def _ready_config() -> Config:
    """A `Config` object that passes every local WhatsApp readiness rule."""
    cfg = Config()
    cfg.site.hostname = "assistant.example.com"
    cfg.whatsapp = WhatsAppConfig(
        enabled=True,
        waba_id="123456789012345",
        phone_number_id="223456789012345",
        business_phone_number="+15551234567",
        access_token="wa-access-token",
        app_secret="wa-app-secret",
        verify_token="wa-verify-token",
        business_timezone="America/Los_Angeles",
    )
    return cfg


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestWhatsAppConfig:
    def test_complete_free_guard_config_loads(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _valid_whatsapp_config()))

        assert cfg.whatsapp.enabled is True
        assert cfg.whatsapp.waba_id == "123456789012345"
        assert cfg.whatsapp.phone_number_id == "223456789012345"
        assert cfg.whatsapp.business_phone_number == "+15551234567"
        assert cfg.whatsapp.billing_policy == "free_guard"
        assert cfg.whatsapp.monthly_service_attempt_limit == 900
        assert cfg.whatsapp.business_timezone == "America/Los_Angeles"
        assert cfg.whatsapp.proactive_template.enabled is False

    def test_complete_paid_config_with_a_template_loads(self, tmp_path):
        body = (
            _valid_whatsapp_config()
            .replace('billing_policy = "free_guard"', 'billing_policy = "allow_paid"')
            .replace(
                'enabled = false\nname = ""\nlanguage = "en_US"',
                'enabled = true\nname = "istota_result"\nlanguage = "en_US"',
            )
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.whatsapp.billing_policy == "allow_paid"
        assert cfg.whatsapp.proactive_template.enabled is True
        assert cfg.whatsapp.proactive_template.name == "istota_result"
        assert cfg.whatsapp.proactive_template.language == "en_US"

    def test_the_three_secrets_load_from_the_environment(self, tmp_path, monkeypatch):
        """The Ansible/Docker/standalone route in: `secrets.env` and friends
        deliver these, and `config.toml` renders them empty on purpose."""
        monkeypatch.setenv("ISTOTA_WHATSAPP_ACCESS_TOKEN", "access-from-env")
        monkeypatch.setenv("ISTOTA_WHATSAPP_APP_SECRET", "secret-from-env")
        monkeypatch.setenv("ISTOTA_WHATSAPP_VERIFY_TOKEN", "verify-from-env")
        body = (
            _valid_whatsapp_config()
            .replace('access_token = "wa-access-token"\n', "")
            .replace('app_secret = "wa-app-secret"\n', "")
            .replace('verify_token = "wa-verify-token"\n', "")
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.whatsapp.access_token == "access-from-env"
        assert cfg.whatsapp.app_secret == "secret-from-env"
        assert cfg.whatsapp.verify_token == "verify-from-env"

    @pytest.mark.parametrize(
        "policy", ["", "free", "paid", "allow-paid", "FREE_GUARD", "unlimited"],
    )
    def test_an_unknown_billing_policy_fails_the_load(self, tmp_path, policy):
        """One of the stage's four named starting failures.

        The policy decides whether a send may ever cost money, so a value the
        code does not recognise must not resolve to a default in either
        direction — `free_guard` would silently disable a paid mode the
        operator asked for, and `allow_paid` would silently authorise spend.
        """
        body = _valid_whatsapp_config().replace(
            'billing_policy = "free_guard"', f'billing_policy = "{policy}"'
        )

        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, body))

    def test_an_unknown_billing_policy_fails_even_while_disabled(self, tmp_path):
        """`sms.provider` learned this: a misspelling in a *disabled* block
        loaded cleanly and then killed a process that built the registry
        unconditionally."""
        body = _valid_whatsapp_config().replace(
            "enabled = true", "enabled = false"
        ).replace('billing_policy = "free_guard"', 'billing_policy = "gratis"')

        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, body))

    @pytest.mark.parametrize(
        "old, new",
        [
            ('waba_id = "123456789012345"', 'waba_id = ""'),
            ('waba_id = "123456789012345"', 'waba_id = "waba-123"'),
            ('phone_number_id = "223456789012345"', 'phone_number_id = ""'),
            ('phone_number_id = "223456789012345"', 'phone_number_id = "+15551234567"'),
            ('business_phone_number = "+15551234567"', 'business_phone_number = ""'),
            (
                'business_phone_number = "+15551234567"',
                'business_phone_number = "555-123-4567"',
            ),
            ('business_timezone = "America/Los_Angeles"', 'business_timezone = ""'),
            (
                'business_timezone = "America/Los_Angeles"',
                'business_timezone = "Mars/Olympus_Mons"',
            ),
            ("request_timeout_seconds = 10", "request_timeout_seconds = 0"),
            ("request_timeout_seconds = 10", "request_timeout_seconds = 31"),
            ("monthly_service_attempt_limit = 900", "monthly_service_attempt_limit = 0"),
            (
                "monthly_service_attempt_limit = 900",
                "monthly_service_attempt_limit = 1001",
            ),
            ('graph_api_version = ""', 'graph_api_version = "23.0"'),
            ('graph_api_version = ""', 'graph_api_version = "latest"'),
        ],
    )
    def test_enabled_config_rejects_invalid_structural_values(self, tmp_path, old, new):
        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, _valid_whatsapp_config().replace(old, new)))

    @pytest.mark.parametrize("version", ["v23", "v23.0", ""])
    def test_an_explicit_graph_version_is_accepted_with_its_v(self, tmp_path, version):
        body = _valid_whatsapp_config().replace(
            'graph_api_version = ""', f'graph_api_version = "{version}"'
        )

        assert load_config(_write_config(tmp_path, body)).whatsapp.graph_api_version == version

    def test_a_template_is_refused_in_free_guard_mode(self, tmp_path):
        body = _valid_whatsapp_config().replace(
            'enabled = false\nname = ""\nlanguage = "en_US"',
            'enabled = true\nname = "istota_result"\nlanguage = "en_US"',
        )

        with pytest.raises(ValueError, match="allow_paid"):
            load_config(_write_config(tmp_path, body))

    @pytest.mark.parametrize(
        "template",
        [
            'enabled = true\nname = ""\nlanguage = "en_US"',
            'enabled = true\nname = "istota_result"\nlanguage = ""',
            'enabled = true\nname = "Istota Result"\nlanguage = "en_US"',
            'enabled = true\nname = "istota_result"\nlanguage = "english"',
        ],
    )
    def test_partial_or_malformed_template_config_is_refused(self, tmp_path, template):
        body = (
            _valid_whatsapp_config()
            .replace('billing_policy = "free_guard"', 'billing_policy = "allow_paid"')
            .replace('enabled = false\nname = ""\nlanguage = "en_US"', template)
        )

        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, body))

    def test_paid_mode_accepts_an_unlimited_local_cap(self, tmp_path):
        body = (
            _valid_whatsapp_config()
            .replace('billing_policy = "free_guard"', 'billing_policy = "allow_paid"')
            .replace(
                "monthly_service_attempt_limit = 900",
                "monthly_service_attempt_limit = 0",
            )
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.whatsapp.monthly_service_attempt_limit == 0

    def test_paid_mode_still_refuses_a_negative_cap(self, tmp_path):
        body = (
            _valid_whatsapp_config()
            .replace('billing_policy = "free_guard"', 'billing_policy = "allow_paid"')
            .replace(
                "monthly_service_attempt_limit = 900",
                "monthly_service_attempt_limit = -1",
            )
        )

        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, body))

    @pytest.mark.parametrize(
        "name",
        ["x" * 300, "/etc/passwd", "../../etc/passwd", "leapseconds", ""],
    )
    def test_a_hostile_timezone_name_is_refused_rather_than_raised(
        self, tmp_path, name
    ):
        """`ZoneInfo` resolves the value as a path under the tzdata
        directories, so a long name raises `ENAMETOOLONG` rather than
        `ZoneInfoNotFoundError` — which escaped `load_config` as a traceback
        carrying the venv path."""
        body = _valid_whatsapp_config().replace(
            'business_timezone = "America/Los_Angeles"',
            f'business_timezone = "{name}"',
        )

        with pytest.raises(ValueError, match="WhatsApp"):
            load_config(_write_config(tmp_path, body))

    def test_missing_credentials_are_reported_rather_than_failing_the_load(
        self, tmp_path
    ):
        """Same rule as `sms_credential_errors`, for the same deployment shape.

        Under `istota_use_environment_file` the Ansible role renders the
        secrets empty on purpose and delivers them through
        `/etc/<ns>/secrets.env`, which a plain `command:`/`script:` task cannot
        read. Raising here fails the play on a config the daemon would have
        loaded fine.
        """
        body = (
            _valid_whatsapp_config()
            .replace('access_token = "wa-access-token"', 'access_token = ""')
            .replace('app_secret = "wa-app-secret"', 'app_secret = ""')
            .replace('verify_token = "wa-verify-token"', 'verify_token = ""')
        )

        cfg = load_config(_write_config(tmp_path, body))

        assert cfg.whatsapp.enabled is True
        assert whatsapp_structural_config_errors(cfg) == []
        errors = " ".join(whatsapp_config_errors(cfg))
        assert "access_token" in errors
        assert "app_secret" in errors
        assert "verify_token" in errors

    def test_a_disabled_block_keeps_the_documented_defaults(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, "[whatsapp]\n"))

        assert cfg.whatsapp.enabled is False
        assert cfg.whatsapp.billing_policy == "free_guard"
        assert cfg.whatsapp.monthly_service_attempt_limit == 900
        assert cfg.whatsapp.request_timeout_seconds == 10
        assert cfg.whatsapp.business_timezone == "UTC"
        assert cfg.whatsapp.graph_api_version == ""
        assert cfg.whatsapp.proactive_template.language == "en_US"

    def test_the_example_config_documents_the_section(self):
        text = (REPO / "config" / "config.example.toml").read_text()
        assert "[whatsapp]" in text
        assert "[whatsapp.proactive_template]" in text


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


class TestPyWaDependency:
    def test_the_pin_is_a_bounded_four_x_range_without_the_server_extra(self):
        manifest = tomllib.loads((REPO / "pyproject.toml").read_text())
        extras = manifest["project"]["optional-dependencies"]

        assert extras["whatsapp"] == ["pywa>=4.4,<5"]
        assert "istota[whatsapp]" in extras["all"]
        assert "istota[whatsapp]" in extras["test"]

    def test_the_installed_release_is_the_four_x_line(self):
        import pywa

        assert pywa.__version__.startswith("4.")


# ---------------------------------------------------------------------------
# Local client types
# ---------------------------------------------------------------------------


class TestWhatsAppTypes:
    def test_the_normalized_records_are_frozen(self):
        from istota.transport.whatsapp._types import (
            InboundWhatsAppEvent,
            WhatsAppSendFailure,
            WhatsAppUserIdentity,
        )

        identity = WhatsAppUserIdentity(bsuid="US.1", wa_id="15551234567", username=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            identity.bsuid = "US.2"

        assert dataclasses.is_dataclass(InboundWhatsAppEvent)
        failure = WhatsAppSendFailure(
            definite=False, error_code=None, safe_reason="delivery state unknown",
        )
        assert failure.definite is False

    def test_a_send_result_is_acceptance_and_never_delivery(self):
        from istota.transport.whatsapp._types import WhatsAppSendResult

        assert WhatsAppSendResult(message_id="wamid.1").status == "accepted"

    def test_the_ledger_states_name_every_local_terminal(self):
        from istota.transport.whatsapp._types import (
            LOCAL_TERMINAL_STATES,
            REACHED_META,
        )

        assert LOCAL_TERMINAL_STATES == frozenset({
            "window_closed", "budget_exhausted", "billing_blocked",
            "opted_out", "unconfigured", "unknown",
        })
        assert REACHED_META == frozenset({"accepted", "sent", "delivered", "read"})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestWhatsAppSchemaMigration:
    def _partial_unique_indexes(self, conn, table: str) -> dict[str, int]:
        return {
            row[1]: row[4]
            for row in conn.execute(f"PRAGMA index_list({table})")
        }

    def test_an_old_database_gains_every_table_and_partial_unique_index(self, tmp_path):
        """One of the stage's four named starting failures.

        The database on the deployment host predates all four tables, so the
        claim is about `init_db` on an *existing* file, not about a fresh one.
        """
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE user_profiles ("
                "user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("INSERT INTO user_profiles (user_id) VALUES ('alice')")

        db.init_db(path)

        with db.get_db(path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            binding_indexes = self._partial_unique_indexes(conn, "whatsapp_user_bindings")
            processed = {r[1] for r in conn.execute("PRAGMA table_info(processed_whatsapp)")}
            sent = {r[1] for r in conn.execute("PRAGMA table_info(sent_whatsapp)")}
            runtime = {r[1] for r in conn.execute("PRAGMA table_info(whatsapp_runtime)")}
            binding = {r[1] for r in conn.execute("PRAGMA table_info(whatsapp_user_bindings)")}

        assert {
            "whatsapp_user_bindings", "processed_whatsapp",
            "sent_whatsapp", "whatsapp_runtime",
        } <= tables
        for name in (
            "idx_whatsapp_binding_phone",
            "idx_whatsapp_binding_bsuid",
            "idx_whatsapp_binding_send_id",
        ):
            assert binding_indexes[name] == 1, f"{name} is not unique"
        assert {
            "user_id", "bootstrap_phone_number", "bsuid", "send_id", "username",
            "opted_out_at", "last_user_message_at", "enrolled_at", "last_seen_at",
            "updated_at",
        } == binding
        assert {
            "id", "message_id", "user_id", "task_id", "disposition",
            "message_type", "received_at",
        } == processed
        assert {
            "logical_key", "meta_message_id", "send_kind", "status", "error_code",
            "body_chars", "body_sha256", "quota_month", "billable",
            "pricing_model", "pricing_category", "pricing_type",
            "claimed_at", "attempted_at",
        } <= sent
        assert runtime == {
            "singleton", "billing_blocked_at", "billing_message_id", "updated_at",
        }

    def test_the_ledger_keeps_no_body_or_destination(self, tmp_path):
        """The task, notification or command record already owns the content,
        and the destination is resolved immediately before the send."""
        path = tmp_path / "fresh.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            sent = {r[1] for r in conn.execute("PRAGMA table_info(sent_whatsapp)")}
            processed = {r[1] for r in conn.execute("PRAGMA table_info(processed_whatsapp)")}

        assert not {"body", "text", "to", "recipient_id", "send_id", "bsuid"} & sent
        assert not {"body", "text", "bsuid", "from_number", "username"} & processed

    def test_the_ledger_refuses_two_rows_for_one_logical_output(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            for _ in range(2):
                try:
                    conn.execute(
                        "INSERT INTO sent_whatsapp (logical_key, user_id, send_kind, "
                        "status, body_chars, body_sha256, created_at, updated_at) "
                        "VALUES ('task-result:7', 'alice', 'service', 'pending', 3, "
                        "'abc', datetime('now'), datetime('now'))"
                    )
                except sqlite3.IntegrityError as exc:
                    assert "logical_key" in str(exc)
                    break
            else:
                pytest.fail("a second row for one logical output was accepted")

    def test_an_inbound_message_id_can_be_claimed_only_once(self, tmp_path):
        """The whole of inbound deduplication rests on this one constraint."""
        path = tmp_path / "fresh.db"
        db.init_db(path)
        insert = (
            "INSERT INTO processed_whatsapp (message_id, user_id, disposition, "
            "message_type, received_at) VALUES ('wamid.1', 'alice', 'received', "
            "'text', datetime('now'))"
        )

        with db.get_db(path) as conn:
            conn.execute(insert)
            with pytest.raises(sqlite3.IntegrityError, match="message_id"):
                conn.execute(insert)

    def test_one_meta_message_id_maps_to_one_ledger_row(self, tmp_path):
        """A status webhook is matched to a row by this id, so two rows
        carrying it would make the delivery state ambiguous. Rows with no id
        yet are the ordinary pre-send state and must not collide."""
        path = tmp_path / "fresh.db"
        db.init_db(path)

        def row(key, meta_id):
            return (
                "INSERT INTO sent_whatsapp (logical_key, meta_message_id, user_id, "
                "send_kind, status, body_chars, body_sha256, created_at, updated_at) "
                f"VALUES ('{key}', {meta_id}, 'alice', 'service', 'pending', 3, "
                "'abc', datetime('now'), datetime('now'))"
            )

        with db.get_db(path) as conn:
            conn.execute(row("a", "NULL"))
            conn.execute(row("b", "NULL"))
            conn.execute(row("c", "'wamid.1'"))
            with pytest.raises(sqlite3.IntegrityError, match="meta_message_id"):
                conn.execute(row("d", "'wamid.1'"))

    def test_the_runtime_circuit_is_a_singleton(self, tmp_path):
        path = tmp_path / "fresh.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO whatsapp_runtime (singleton, updated_at) "
                    "VALUES (2, datetime('now'))"
                )


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


class TestWhatsAppBindingStore:
    def _db(self, tmp_path) -> Path:
        path = tmp_path / "istota.db"
        db.init_db(path)
        return path

    def test_a_bootstrap_number_enrolls_and_reads_back(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding is not None
        assert binding.user_id == "alice"
        assert binding.bootstrap_phone_number == "+15551234567"
        assert binding.bsuid == ""
        assert binding.send_id == ""
        assert binding.enrolled_at is None

    def test_an_explicit_bsuid_can_be_assigned_with_the_number(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number="+15551234567",
                bsuid="US.1234567890",
            )
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.bsuid == "US.1234567890"
        assert binding.enrolled_at is not None

    def test_changing_the_number_clears_a_learned_identity(self, tmp_path):
        """A recycled or reassigned bootstrap number must not carry the old
        principal's WhatsApp identity with it."""
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number="+15551234567",
                bsuid="US.1234567890",
                send_id="send-1",
            )
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15557654321")
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.bootstrap_phone_number == "+15557654321"
        assert binding.bsuid == ""
        assert binding.send_id == ""

    def test_changing_the_number_keeps_a_bsuid_supplied_in_the_same_call(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15551234567", bsuid="US.1",
            )
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15557654321", bsuid="US.2",
            )
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.bootstrap_phone_number == "+15557654321"
        assert binding.bsuid == "US.2"

    def _bind_with_state(self, conn, user_id="alice", **fields):
        """A binding carrying the state a live conversation leaves behind."""
        db.set_whatsapp_binding(conn, user_id, **fields)
        conn.execute(
            "UPDATE whatsapp_user_bindings SET last_user_message_at = ?, "
            "last_seen_at = ?, opted_out_at = ? WHERE user_id = ?",
            ("2026-09-11 10:00:00", "2026-09-11 10:00:00",
             "2026-09-01 00:00:00", user_id),
        )

    @pytest.mark.parametrize(
        "change",
        [
            {"bootstrap_phone_number": "+15557654321"},
            {"bsuid": "US.2"},
            {"bootstrap_phone_number": "+15557654321", "bsuid": "US.2"},
            {"bootstrap_phone_number": "+15557654321", "bsuid": ""},
        ],
        ids=["number", "bsuid", "both", "number-and-cleared-bsuid"],
    )
    def test_an_identity_change_discards_the_previous_holders_state(
        self, tmp_path, change
    ):
        """`last_user_message_at` is the one that matters: it is the sole
        input to the 24-hour service window, so inheriting it would let a
        free-form message go to somebody who never wrote in. `send_id` is a
        principal, and the opt-out belongs to whoever set it."""
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            self._bind_with_state(
                conn,
                bootstrap_phone_number="+15551234567",
                bsuid="US.1",
                send_id="send-1",
                username="old-name",
            )
            db.set_whatsapp_binding(conn, "alice", **change)
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.send_id == ""
        assert binding.username == ""
        assert binding.last_user_message_at is None
        assert binding.last_seen_at is None
        assert binding.opted_out_at is None

    def test_an_unchanged_identity_keeps_its_conversation_state(self, tmp_path):
        """The control for the case above: latching a send id on an inbound
        event must not reset the window it was just observed through."""
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            self._bind_with_state(
                conn, bootstrap_phone_number="+15551234567", bsuid="US.1",
            )
            db.set_whatsapp_binding(conn, "alice", send_id="send-1")
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.send_id == "send-1"
        assert binding.last_user_message_at == "2026-09-11 10:00:00"
        assert binding.opted_out_at == "2026-09-01 00:00:00"

    def test_a_replacement_bsuid_is_a_new_enrollment(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15551234567", bsuid="US.1",
            )
            conn.execute(
                "UPDATE whatsapp_user_bindings SET enrolled_at = '2020-01-01 00:00:00' "
                "WHERE user_id = 'alice'"
            )
            db.set_whatsapp_binding(conn, "alice", bsuid="US.2")
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.enrolled_at != "2020-01-01 00:00:00"

    def test_unsetting_the_number_with_no_identity_left_is_refused(self, tmp_path):
        """`--whatsapp-number ""` would otherwise leave a row naming a user
        and no way to reach or recognise them, which is what
        `--clear-whatsapp` is for."""
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
            with pytest.raises(ValueError, match="clear-whatsapp"):
                db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="")
            assert (
                db.get_whatsapp_binding(conn, "alice").bootstrap_phone_number
                == "+15551234567"
            )

    def test_reset_clears_only_the_learned_identity(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice",
                bootstrap_phone_number="+15551234567",
                bsuid="US.1",
                send_id="send-1",
            )
            db.reset_whatsapp_identity(conn, "alice")
            binding = db.get_whatsapp_binding(conn, "alice")

        assert binding.bootstrap_phone_number == "+15551234567"
        assert binding.bsuid == ""
        assert binding.send_id == ""
        assert binding.enrolled_at is None
        # The same discard set `set_whatsapp_binding` applies, so the two
        # paths cannot disagree about what a learned identity is.
        assert binding.last_user_message_at is None
        assert binding.opted_out_at is None

    def test_reset_refuses_a_binding_with_no_bootstrap_number_left(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", bsuid="US.1")
            with pytest.raises(ValueError, match="bootstrap"):
                db.reset_whatsapp_identity(conn, "alice")

    def test_clear_removes_the_whole_binding(self, tmp_path):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
            assert db.clear_whatsapp_binding(conn, "alice") is True
            assert db.get_whatsapp_binding(conn, "alice") is None
            assert db.clear_whatsapp_binding(conn, "alice") is False

    @pytest.mark.parametrize(
        "field, first, second, message",
        [
            ("bootstrap_phone_number", "+15551234567", "+15551234567", "phone number"),
            ("bsuid", "US.1234567890", "US.1234567890", "BSUID"),
            ("send_id", "send-1", "send-1", "send id"),
        ],
    )
    def test_each_identity_is_unique_across_users(
        self, tmp_path, field, first, second, message
    ):
        """One of the stage's four named starting failures.

        Each of these three decides which Istota user an authenticated inbound
        event may act as, so a second user holding the same value is a
        principal takeover rather than a duplicate row.
        """
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", **{field: first})
            with pytest.raises(ValueError, match=message) as excinfo:
                db.set_whatsapp_binding(conn, "bob", **{field: second})

        assert "UNIQUE constraint" not in str(excinfo.value)
        with db.get_db(path) as conn:
            assert db.get_whatsapp_binding(conn, "bob") is None

    def test_unbound_users_do_not_collide_on_empty_identities(self, tmp_path):
        """The partial indexes exist for exactly this: `''` is the unbound
        value on three columns at once, and a plain UNIQUE would let the
        second user with no identity fail."""
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", username="alice-wa")
            db.set_whatsapp_binding(conn, "bob", username="bob-wa")

            assert db.get_whatsapp_binding(conn, "alice").username == "alice-wa"
            assert db.get_whatsapp_binding(conn, "bob").username == "bob-wa"

    def test_deleting_a_profile_takes_the_binding_with_it(self, tmp_path):
        """An orphan binding keeps a deleted user's number and BSUID reserved
        and leaves a live principal an inbound event can still resolve to.
        `sms_phone_number` needs no equivalent: it is a column on the row."""
        path = self._db(tmp_path)
        user_profiles.ensure_profile(path, "alice")
        with db.get_db(path) as conn:
            db.set_whatsapp_binding(
                conn, "alice", bootstrap_phone_number="+15551234567", bsuid="US.1",
            )

        assert user_profiles.delete_profile(path, "alice") is True

        with db.get_db(path) as conn:
            assert db.get_whatsapp_binding(conn, "alice") is None
            # And the identity is free for whoever the operator binds next.
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15551234567")

    @pytest.mark.parametrize(
        "number",
        ["15551234567", "+05551234567", "+1555 123 4567", "+1234567",
         "+1234567890123456"],
    )
    def test_a_bootstrap_number_is_exact_e164_or_refused(self, tmp_path, number):
        path = self._db(tmp_path)

        with db.get_db(path) as conn:
            with pytest.raises(ValueError, match="E.164"):
                db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number=number)


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


class TestWhatsAppMasking:
    def test_a_phone_number_keeps_only_its_last_four_digits(self):
        assert user_profiles.mask_phone_number("+15551234567") == "+*******4567"
        assert user_profiles.mask_sms_phone_number("+15551234567") == "+*******4567"
        assert user_profiles.mask_phone_number("") == ""

    def test_an_identifier_is_shown_as_a_one_way_fingerprint(self):
        fingerprint = user_profiles.mask_whatsapp_identifier("US.1234567890")

        assert fingerprint
        assert "1234567890" not in fingerprint
        assert len(fingerprint) <= 16
        assert fingerprint == user_profiles.mask_whatsapp_identifier("US.1234567890")
        assert fingerprint != user_profiles.mask_whatsapp_identifier("US.1234567891")
        assert user_profiles.mask_whatsapp_identifier("") == ""


# ---------------------------------------------------------------------------
# Operator commands
# ---------------------------------------------------------------------------


def _ensure_args(config_path: Path, **overrides) -> SimpleNamespace:
    defaults = {
        "config": str(config_path),
        "name": "alice",
        "display_name": None,
        "tz": None,
        "email": None,
        "trusted_sender": None,
        "quiet_sender": None,
        "log_channel": None,
        "alerts_channel": None,
        "max_foreground_workers": None,
        "max_background_workers": None,
        "disabled_skill": None,
        "disabled_module": None,
        "default_destination": None,
        "default_room": None,
        "route": None,
        "email_reply_routing": None,
        "outbound_approval": None,
        "external_turn_display": None,
        "default_briefings": None,
        "briefing_email_html": None,
        "timezone_follow_location": None,
        "sms_number": None,
        "clear_sms_number": False,
        "whatsapp_number": None,
        "whatsapp_bsuid": None,
        "clear_whatsapp": False,
        "reset_whatsapp_identity": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestWhatsAppOperatorCommands:
    def _setup(self, tmp_path) -> tuple[Path, Path]:
        path = tmp_path / "istota.db"
        db.init_db(path)
        config_path = _write_config(tmp_path, f'db_path = "{path}"\n')
        return path, config_path

    def test_user_ensure_enrolls_and_prints_nothing_identifying(self, tmp_path, capsys):
        from istota.cli import cmd_user_ensure

        path, config_path = self._setup(tmp_path)

        cmd_user_ensure(_ensure_args(
            config_path,
            whatsapp_number="+15551234567",
            whatsapp_bsuid="US.1234567890",
        ))
        output = capsys.readouterr().out

        assert "+15551234567" not in output
        assert "US.1234567890" not in output
        assert "+*******4567" in output
        with db.get_db(path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bootstrap_phone_number == "+15551234567"
        assert binding.bsuid == "US.1234567890"

    def test_user_ensure_resets_and_clears(self, tmp_path, capsys):
        from istota.cli import cmd_user_ensure

        path, config_path = self._setup(tmp_path)
        cmd_user_ensure(_ensure_args(
            config_path,
            whatsapp_number="+15551234567",
            whatsapp_bsuid="US.1234567890",
        ))

        cmd_user_ensure(_ensure_args(config_path, reset_whatsapp_identity=True))
        with db.get_db(path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bootstrap_phone_number == "+15551234567"
        assert binding.bsuid == ""

        cmd_user_ensure(_ensure_args(config_path, clear_whatsapp=True))
        with db.get_db(path) as conn:
            assert db.get_whatsapp_binding(conn, "alice") is None
        capsys.readouterr()

    def test_a_bad_number_exits_without_writing(self, tmp_path):
        from istota.cli import cmd_user_ensure

        path, config_path = self._setup(tmp_path)

        with pytest.raises(SystemExit):
            cmd_user_ensure(_ensure_args(config_path, whatsapp_number="555-1234"))

        with db.get_db(path) as conn:
            assert db.get_whatsapp_binding(conn, "alice") is None

    def test_a_database_without_the_tables_does_not_traceback(self, tmp_path):
        """`user ensure` reads the binding on every invocation, whatever flags
        were passed, and the Ansible role runs it per user on every deploy. A
        checkout that lands before the migration must not crash a command that
        has already written the profile row."""
        from istota.cli import cmd_user_ensure, cmd_user_show

        path, config_path = self._setup(tmp_path)
        with db.get_db(path) as conn:
            for table in (
                "whatsapp_user_bindings", "processed_whatsapp",
                "sent_whatsapp", "whatsapp_runtime",
            ):
                conn.execute(f"DROP TABLE {table}")

        cmd_user_ensure(_ensure_args(config_path, display_name="Alice"))
        cmd_user_show(SimpleNamespace(config=str(config_path), name="alice"))

    def test_user_show_returns_the_complete_binding_for_operator_automation(
        self, tmp_path, capsys
    ):
        """`user show` already returns the full `sms_phone_number`; it is the
        private operator surface the spec exempts."""
        from istota.cli import cmd_user_ensure, cmd_user_show

        path, config_path = self._setup(tmp_path)
        cmd_user_ensure(_ensure_args(
            config_path, whatsapp_number="+15551234567", whatsapp_bsuid="US.1",
        ))
        capsys.readouterr()

        cmd_user_show(SimpleNamespace(config=str(config_path), name="alice"))
        payload = json.loads(capsys.readouterr().out)

        assert payload["whatsapp"]["bootstrap_phone_number"] == "+15551234567"
        assert payload["whatsapp"]["bsuid"] == "US.1"

    @pytest.mark.parametrize(
        "flags",
        [
            ["--whatsapp-number", "+15551234567", "--clear-whatsapp"],
            ["--whatsapp-bsuid", "US.1", "--clear-whatsapp"],
            ["--clear-whatsapp", "--reset-whatsapp-identity"],
            ["--whatsapp-number", "+15551234567", "--reset-whatsapp-identity"],
        ],
    )
    def test_conflicting_assignment_flags_are_refused_by_the_parser(
        self, monkeypatch, flags
    ):
        from istota import cli

        monkeypatch.setattr(
            sys, "argv", ["istota", "user", "ensure", "--name", "alice", *flags],
        )
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2

    def test_number_and_bsuid_may_be_supplied_together(self, monkeypatch, tmp_path):
        """Explicit enrollment is the documented shape for a username-only
        user, who cannot bootstrap by phone."""
        from istota import cli

        path, config_path = self._setup(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "istota", "-c", str(config_path), "user", "ensure", "--name", "alice",
            "--whatsapp-number", "+15551234567", "--whatsapp-bsuid", "US.1",
        ])

        cli.main()

        with db.get_db(path) as conn:
            binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bsuid == "US.1"


class TestWhatsAppBillingCircuit:
    def test_the_circuit_starts_open_and_unblock_clears_a_recorded_block(self, tmp_path):
        path = tmp_path / "istota.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            assert db.whatsapp_billing_block(conn) is None
            db.block_whatsapp_billing(conn, "wamid.first-billable")
            blocked = db.whatsapp_billing_block(conn)

        assert blocked is not None
        assert blocked.billing_message_id == "wamid.first-billable"

        with db.get_db(path) as conn:
            assert db.clear_whatsapp_billing_block(conn) is True
            assert db.whatsapp_billing_block(conn) is None
            assert db.clear_whatsapp_billing_block(conn) is False

    def test_a_second_block_does_not_overwrite_the_first_evidence(self, tmp_path):
        path = tmp_path / "istota.db"
        db.init_db(path)

        with db.get_db(path) as conn:
            db.block_whatsapp_billing(conn, "wamid.first")
            db.block_whatsapp_billing(conn, "wamid.second")
            blocked = db.whatsapp_billing_block(conn)

        assert blocked.billing_message_id == "wamid.first"

    def test_the_cli_unblocks_and_reports_the_evidence(self, tmp_path, capsys):
        """The private operator command shows the Meta message id in full.
        It is the only place the id is readable — this same call clears the
        row it lives on — and it is what the operator matches against the
        Meta billing page. Every general surface fingerprints it instead."""
        from istota.cli import cmd_whatsapp_billing_unblock

        path = tmp_path / "istota.db"
        db.init_db(path)
        config_path = _write_config(tmp_path, f'db_path = "{path}"\n')
        with db.get_db(path) as conn:
            db.block_whatsapp_billing(conn, "wamid.evidence")

        cmd_whatsapp_billing_unblock(SimpleNamespace(config=str(config_path)))
        output = capsys.readouterr().out

        assert "wamid.evidence" in output
        with db.get_db(path) as conn:
            assert db.whatsapp_billing_block(conn) is None

    def test_unblocking_an_open_circuit_says_so_without_inventing_evidence(
        self, tmp_path, capsys
    ):
        from istota.cli import cmd_whatsapp_billing_unblock

        path = tmp_path / "istota.db"
        db.init_db(path)
        config_path = _write_config(tmp_path, f'db_path = "{path}"\n')

        cmd_whatsapp_billing_unblock(SimpleNamespace(config=str(config_path)))

        assert "not blocked" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Redaction and masking in operator surfaces
# ---------------------------------------------------------------------------


class TestWhatsAppSecretRedaction:
    @pytest.mark.parametrize(
        "dotted", ["whatsapp.access_token", "whatsapp.app_secret", "whatsapp.verify_token"],
    )
    def test_every_credential_is_redacted(self, dotted):
        """One of the stage's four named starting failures."""
        cfg = Config()
        target = cfg
        *path, leaf = dotted.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, leaf, "WHATSAPP-SENTINEL-SECRET")

        payload = admin_config_view.build_config_view(cfg)
        fields = {
            field["key"]: field
            for section in payload["sections"]
            for field in section["fields"]
        }

        assert "WHATSAPP-SENTINEL-SECRET" not in json.dumps(payload, default=str)
        assert fields[dotted]["secret"] is True
        assert fields[dotted]["set"] is True

    def test_the_business_number_is_masked_rather_than_rendered(self):
        cfg = Config()
        cfg.whatsapp = WhatsAppConfig(
            enabled=True,
            waba_id="123456789012345",
            phone_number_id="223456789012345",
            business_phone_number="+15551234567",
            access_token="wa-access-token",
            app_secret="wa-app-secret",
            verify_token="wa-verify-token",
        )

        payload = admin_config_view.build_config_view(cfg)
        rendered = json.dumps(payload, default=str)
        fields = {
            field["key"]: field
            for section in payload["sections"]
            for field in section["fields"]
        }

        assert "+15551234567" not in rendered
        assert fields["whatsapp.business_phone_number"]["value"] == "+*******4567"
        assert "wa-access-token" not in rendered
        assert "wa-app-secret" not in rendered
        assert "wa-verify-token" not in rendered

    def test_the_identifiers_stay_visible_to_the_operator(self):
        """WABA and phone number ids are identifiers rather than secrets. The
        admin configuration page is the operator's own surface, and an id they
        cannot read is an id they cannot check against the Meta portal."""
        cfg = Config()
        cfg.whatsapp = WhatsAppConfig(waba_id="123456789012345")

        fields = {
            field["key"]: field
            for section in admin_config_view.build_config_view(cfg)["sections"]
            for field in section["fields"]
        }

        assert fields["whatsapp.waba_id"]["value"] == "123456789012345"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class TestWhatsAppDoctorReadiness:
    def _results(self, cfg):
        return {
            result.name: result
            for result in doctor.run_checks(cfg, only=("whatsapp.",), probe=False)
        }

    def test_a_disabled_transport_is_skipped(self):
        results = self._results(Config())

        assert results
        assert all(result.status == doctor.SKIP for result in results.values())

    def test_a_complete_configuration_reports_ready(self):
        results = self._results(_ready_config())

        assert results["whatsapp.common"].status == doctor.OK
        detail = results["whatsapp.common"].detail.lower()
        assert "free_guard" in detail
        assert "deliver" not in detail

    def test_missing_fields_are_named_without_their_values(self):
        cfg = _ready_config()
        cfg.whatsapp.access_token = ""
        cfg.whatsapp.app_secret = ""
        cfg.whatsapp.business_phone_number = "+15551234567"

        result = self._results(cfg)["whatsapp.common"]

        assert result.status == doctor.FAIL
        assert "access_token" in result.detail
        assert "app_secret" in result.detail
        assert "+15551234567" not in str(result)
        assert result.remedy

    def test_a_configured_secret_is_never_rendered(self):
        cfg = _ready_config()
        cfg.whatsapp.verify_token = "WHATSAPP-SENTINEL-SECRET"
        cfg.whatsapp.waba_id = ""

        result = self._results(cfg)["whatsapp.common"]

        assert result.status == doctor.FAIL
        assert "WHATSAPP-SENTINEL-SECRET" not in str(result)

    def test_credentials_absent_from_this_process_warn_rather_than_fail(self):
        """The Ansible shape: `config.toml` carries no secret and
        `secrets.env` delivers all three to the units. An operator running
        `istota doctor` from their own shell is reading an environment the
        daemon has and they do not, so a FAIL would report a working
        deployment as broken."""
        cfg = _ready_config()
        cfg.whatsapp.access_token = ""
        cfg.whatsapp.app_secret = ""
        cfg.whatsapp.verify_token = ""

        result = self._results(cfg)["whatsapp.common"]

        assert result.status == doctor.WARN
        assert result.remedy
        assert doctor.verdict([result])[0] is True

    def test_a_partial_credential_set_is_still_a_failure(self):
        """No delivery mechanism supplies one of three, so this is a real
        mistake rather than an environment this process cannot see."""
        cfg = _ready_config()
        cfg.whatsapp.app_secret = ""

        result = self._results(cfg)["whatsapp.common"]

        assert result.status == doctor.FAIL
        assert "app_secret" in result.detail
        assert "access_token" not in result.detail

    def test_the_template_state_is_reported_as_configured_only(self):
        """Meta owns approval, pause and category, so the local check may only
        report what the operator configured — never that a template will send."""
        cfg = _ready_config()
        cfg.whatsapp.billing_policy = "allow_paid"
        cfg.whatsapp.proactive_template = WhatsAppTemplateConfig(
            enabled=True, name="istota_result", language="en_US",
        )

        detail = self._results(cfg)["whatsapp.common"].detail.lower()

        assert "istota_result" in detail
        assert "approved" not in detail

    def test_the_billing_check_reports_a_closed_circuit_and_the_months_spend(
        self, tmp_path,
    ):
        cfg = _ready_config()
        cfg.db_path = tmp_path / "istota.db"
        db.init_db(cfg.db_path)

        result = self._results(cfg)["whatsapp.billing"]

        assert result.status == doctor.OK
        assert "circuit closed" in result.detail
        assert "0 service attempts" in result.detail

    def test_an_open_circuit_warns_and_fingerprints_its_evidence(self, tmp_path):
        """An open circuit refuses every send on a perfectly-configured
        deployment, so `whatsapp.common` reading `ready` says nothing about it.

        `WARN` rather than `FAIL`: the circuit is the guard doing its job, and
        exiting 1 over a deliberate protective trip trains an operator to stop
        reading the exit status. The evidence is fingerprinted because a
        `CheckResult` reaches the boot log and the admin Health pane.
        """
        cfg = _ready_config()
        cfg.db_path = tmp_path / "istota.db"
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.block_whatsapp_billing(conn, "wamid.billable.1")

        result = self._results(cfg)["whatsapp.billing"]

        assert result.status == doctor.WARN
        assert "wamid.billable.1" not in result.detail
        assert "billing-unblock" in result.remedy

    def test_a_missing_database_is_skipped_rather_than_created(self, tmp_path):
        # A diagnostic that opens a database into existence leaves the
        # zero-byte file `check_framework_db` later reports as corruption.
        cfg = _ready_config()
        cfg.db_path = tmp_path / "absent" / "istota.db"

        result = self._results(cfg)["whatsapp.billing"]

        assert result.status == doctor.SKIP
        assert not cfg.db_path.exists()

    def test_an_unreadable_ledger_is_unanswered_rather_than_clear(self, tmp_path):
        # A reader must not call a boundary closed on a question it could not
        # settle: a database with no `sent_whatsapp` table is a migration gap,
        # not evidence that the circuit is shut.
        cfg = _ready_config()
        cfg.db_path = tmp_path / "istota.db"
        cfg.db_path.write_bytes(b"")

        result = self._results(cfg)["whatsapp.billing"]

        assert result.status == doctor.WARN
        assert "could not be read" in result.detail
