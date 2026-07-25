"""Reference guards shared by the web routes and the operator CLI.

The guards live outside the route so the claim "a referenced service can't be
deleted" holds on every surface — an agent reaching for
`istota money service remove` must not be able to unbill a client's work in a
way the browser refuses to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota.money import config_refs, config_store
from istota.money.cli import UserContext
from istota.money.work import add_work_entry


@pytest.fixture
def ctx(tmp_path: Path) -> UserContext:
    data_dir = tmp_path / "money"
    db_path = data_dir / "data" / "money.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(db_path)
    return UserContext(data_dir=data_dir, ledgers=[], db_path=db_path)


class TestServiceReferences:
    def test_referenced_service_is_blocked(self, ctx):
        config_store.upsert_service(ctx.db_path, "dev", display_name="Dev")
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=2)

        scan = config_refs.service_references(ctx.db_path, ctx.data_dir, "dev")
        assert not scan.allowed
        assert scan.references["work_entries"] == 1
        assert "1 work entry" in scan.blocked_reason

    def test_unreferenced_service_is_allowed(self, ctx):
        config_store.upsert_service(ctx.db_path, "design", display_name="Design")
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=2)

        scan = config_refs.service_references(ctx.db_path, ctx.data_dir, "design")
        assert scan.allowed

    def test_no_work_store_at_all_is_allowed(self, ctx):
        """A user who has never invoiced is zero references, not a blocked delete."""
        scan = config_refs.service_references(ctx.db_path, ctx.data_dir, "design")
        assert scan.allowed
        assert scan.references["work_entries"] == 0


class TestEntityReferences:
    def test_client_naming_it_blocks(self, ctx):
        config_store.upsert_company(ctx.db_path, "oldco", name="Old")
        config_store.upsert_company(ctx.db_path, "newco", name="New")
        config_store.upsert_client(ctx.db_path, "acme", name="Acme", entity="oldco")

        scan = config_refs.entity_references(ctx.db_path, ctx.data_dir, "oldco")
        assert not scan.allowed
        assert scan.references["clients"] == ["acme"]

    def test_work_entry_pinning_it_blocks(self, ctx):
        config_store.upsert_company(ctx.db_path, "oldco", name="Old")
        config_store.upsert_company(ctx.db_path, "newco", name="New")
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=1, entity="oldco")

        scan = config_refs.entity_references(ctx.db_path, ctx.data_dir, "oldco")
        assert not scan.allowed
        assert scan.references["work_entries"] == 1

    def test_effective_default_is_the_resolved_company(self, ctx):
        """A dangling stored default falls back to the first company, and that
        is the entity blank-entity clients actually bill under."""
        config_store.upsert_company(ctx.db_path, "acme", name="Acme LLC")
        config_store.upsert_client(ctx.db_path, "globex", name="Globex", entity="")
        cfg = config_store.load_invoicing(ctx.db_path)
        cfg.default_entity = "nonexistent"
        config_store.save_invoicing(ctx.db_path, cfg, replace_collections=False)

        scan = config_refs.entity_references(ctx.db_path, ctx.data_dir, "acme")
        assert not scan.allowed
        assert scan.references["default_for_clients"] == 1

    def test_spare_entity_is_deletable(self, ctx):
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        config_store.upsert_company(ctx.db_path, "spare", name="Spare")
        config_store.upsert_client(ctx.db_path, "acme", name="Acme", entity="main")

        scan = config_refs.entity_references(ctx.db_path, ctx.data_dir, "spare")
        assert scan.allowed


class TestClientReferences:
    def test_never_blocks(self, ctx):
        config_store.upsert_client(ctx.db_path, "acme", name="Acme")
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=1)

        scan = config_refs.client_references(ctx.db_path, ctx.data_dir, "acme")
        assert scan.allowed
        assert scan.references["work_entries"] == 1

    def test_matching_is_case_insensitive(self, ctx):
        """Work entries store the client lowercased, so a legacy mixed-case
        config key would otherwise report zero for entries it plainly owns."""
        add_work_entry(ctx.data_dir, "2026-03-01", "acme", "dev", qty=1)

        scan = config_refs.client_references(ctx.db_path, ctx.data_dir, "Acme")
        assert scan.references["work_entries"] == 1


class TestQuarantineIsNotZero:
    """`_load_year` skips an unreadable row without raising, so it is invisible
    to a count — and a guard built on that count fails open."""

    def _quarantine(self, ctx):
        work_dir = ctx.data_dir / "invoices" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "2026.toml").write_text(
            "[[entries]]\n"
            'date = 2026-03-01\nclient = "acme"\nservice = "dev"\nqty = 1\n\n'
            "[[entries]]\n"
            'date = 2026-03-02\nclient = "acme"\n'
        )

    def test_service_scan_blocks(self, ctx):
        self._quarantine(ctx)
        scan = config_refs.service_references(ctx.db_path, ctx.data_dir, "consulting")
        assert not scan.allowed
        assert scan.references["quarantined"] == ["2026.toml"]

    def test_entity_scan_blocks(self, ctx):
        config_store.upsert_company(ctx.db_path, "main", name="Main")
        config_store.upsert_company(ctx.db_path, "spare", name="Spare")
        self._quarantine(ctx)
        scan = config_refs.entity_references(ctx.db_path, ctx.data_dir, "spare")
        assert not scan.allowed

    def test_client_scan_reports_but_allows(self, ctx):
        self._quarantine(ctx)
        scan = config_refs.client_references(ctx.db_path, ctx.data_dir, "acme")
        assert scan.allowed
        assert scan.references["quarantined"] == ["2026.toml"]


class TestScanFailure:
    def test_unreadable_store_blocks_the_strict_kinds(self, ctx, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("mount is gone")

        monkeypatch.setattr("istota.money.work.load_work_entries", boom)
        scan = config_refs.service_references(ctx.db_path, ctx.data_dir, "dev")
        assert scan.scan_failed
        assert not scan.allowed

    def test_client_scan_degrades_instead(self, ctx, monkeypatch):
        """The soft delete destroys nothing, so refusing would strand the user
        behind a broken year file."""
        def boom(*args, **kwargs):
            raise OSError("mount is gone")

        monkeypatch.setattr("istota.money.work.load_work_entries", boom)
        scan = config_refs.client_references(ctx.db_path, ctx.data_dir, "acme")
        assert scan.scan_failed
        assert scan.references == {}
