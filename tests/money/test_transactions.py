"""Tests for money.core.transactions module."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

from money.core.models import (
    MonarchConfig,
    MonarchCredentials,
    MonarchProfile,
    MonarchSyncSettings,
    MonarchTagFilters,
)
from money.core.transactions import (
    MONARCH_CATEGORY_MAP,
    filter_by_tags,
    format_beancount_transaction,
    format_category_change_entry,
    format_recategorization_entry,
    import_csv,
    map_monarch_category,
    map_monarch_category_with_config,
    map_monarch_account,
    parse_monarch_config,
    parse_monarch_csv,
    parse_tags,
    sync_all_profiles,
    add_transaction,
    backup_ledger,
    append_to_ledger,
)


class TestCategoryMapping:
    def test_exact_match(self):
        assert map_monarch_category("Groceries") == "Expenses:Food:Groceries"
        assert map_monarch_category("Income") == "Income:Salary"

    def test_case_insensitive_match(self):
        assert map_monarch_category("groceries") == "Expenses:Food:Groceries"
        assert map_monarch_category("GROCERIES") == "Expenses:Food:Groceries"

    def test_unknown_category(self):
        result = map_monarch_category("Unknown Category")
        assert result == "Expenses:Uncategorized:UnknownCategory"

    def test_all_mapped_categories_valid(self):
        for category, account in MONARCH_CATEGORY_MAP.items():
            assert ":" in account
            assert account.startswith(("Income:", "Expenses:", "Assets:", "Liabilities:", "Equity:"))

    def test_with_config_overrides(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={},
            categories={"Groceries": "Expenses:Business:Food"},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_category_with_config("Groceries", config) == "Expenses:Business:Food"

    def test_config_fallback_to_builtin(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_category_with_config("Groceries", config) == "Expenses:Food:Groceries"


class TestAccountMapping:
    def test_exact_match(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(),
            accounts={"Chase Checking": "Assets:Bank:Chase"},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_account("Chase Checking", config) == "Assets:Bank:Chase"

    def test_fallback_to_default(self):
        config = MonarchConfig(
            credentials=MonarchCredentials(),
            sync=MonarchSyncSettings(default_account="Assets:Bank:Default"),
            accounts={},
            categories={},
            tags=MonarchTagFilters(),
        )
        assert map_monarch_account("Unknown Account", config) == "Assets:Bank:Default"


class TestTagParsing:
    def test_parse_tags(self):
        assert parse_tags("Business, Travel") == ["Business", "Travel"]
        assert parse_tags("") == []
        assert parse_tags("  ") == []
        assert parse_tags("Single") == ["Single"]

    def test_filter_by_tags_include(self):
        assert filter_by_tags(["Business"], ["Business"], None) is True
        assert filter_by_tags(["Personal"], ["Business"], None) is False

    def test_filter_by_tags_exclude(self):
        assert filter_by_tags(["Personal"], None, ["Personal"]) is False
        assert filter_by_tags(["Business"], None, ["Personal"]) is True

    def test_filter_by_tags_both(self):
        assert filter_by_tags(["Business", "Tax"], ["Business"], ["Tax"]) is False
        assert filter_by_tags(["Business"], ["Business"], ["Tax"]) is True

    def test_filter_no_filters(self):
        assert filter_by_tags(["anything"], None, None) is True


class TestCSVParsing:
    def test_parse_monarch_csv(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase Checking,WHOLE FOODS #123,Weekly groceries,-85.50,Personal,Stefan\n"
            "2026-01-16,Employer,Income,Chase Checking,PAYROLL DEPOSIT,Paycheck,5000.00,Business,Stefan\n"
            "01/17/2026,Amazon,Shopping,Chase Checking,AMAZON.COM,,-42.99,,Stefan\n"
        )
        transactions = parse_monarch_csv(csv_file)
        assert len(transactions) == 3
        assert transactions[0]["date"] == date(2026, 1, 15)
        assert transactions[0]["merchant"] == "Whole Foods"
        assert transactions[0]["amount"] == -85.50
        assert transactions[0]["tags"] == ["Personal"]
        assert transactions[2]["date"] == date(2026, 1, 17)
        assert transactions[2]["tags"] == []

    def test_skips_invalid_dates(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "invalid-date,Store,Shopping,Account,STORE,-10.00,,,\n"
            "2026-01-15,Valid Store,Shopping,Account,VALID STORE,,-20.00,,Stefan\n"
        )
        transactions = parse_monarch_csv(csv_file)
        assert len(transactions) == 1

    def test_include_filter(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Stefan\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Stefan\n"
        )
        transactions = parse_monarch_csv(csv_file, include_tags=["Business"])
        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Store A"

    def test_exclude_filter(self, tmp_path):
        csv_file = tmp_path / "transactions.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Store A,Shopping,Account,STORE A,,-10.00,Business,Stefan\n"
            "2026-01-16,Store B,Shopping,Account,STORE B,,-20.00,Personal,Stefan\n"
        )
        transactions = parse_monarch_csv(csv_file, exclude_tags=["Personal"])
        assert len(transactions) == 1
        assert transactions[0]["merchant"] == "Store A"


class TestBeancountFormatting:
    def test_expense_transaction(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 15), payee="Whole Foods",
            narration="Weekly groceries", posting_account="Expenses:Food:Groceries",
            contra_account="Assets:Bank:Checking", amount=-85.50,
        )
        assert '2026-01-15 * "Whole Foods" "Weekly groceries"' in result
        assert "Expenses:Food:Groceries  85.50 USD" in result
        assert "Assets:Bank:Checking" in result

    def test_income_transaction(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 16), payee="Employer",
            narration="Paycheck", posting_account="Income:Salary",
            contra_account="Assets:Bank:Checking", amount=5000.00,
        )
        assert '2026-01-16 * "Employer" "Paycheck"' in result
        assert "Assets:Bank:Checking  5000.00 USD" in result

    def test_escapes_quotes(self):
        result = format_beancount_transaction(
            txn_date=date(2026, 1, 15), payee='Store "Best"',
            narration='Item "Special"', posting_account="Expenses:Shopping",
            contra_account="Assets:Bank:Checking", amount=-10.00,
        )
        assert '\\"Best\\"' in result
        assert '\\"Special\\"' in result

    def test_recategorization_entry(self):
        result = format_recategorization_entry(
            txn_date=date(2026, 2, 7), merchant="Starbucks",
            original_account="Expenses:Food:Coffee",
            recategorize_account="Expenses:Personal-Expense", amount=5.50,
        )
        assert "Recategorized: business tag removed" in result
        assert "Expenses:Personal-Expense  5.50 USD" in result
        assert "Expenses:Food:Coffee  -5.50 USD" in result

    def test_category_change_entry(self):
        result = format_category_change_entry(
            txn_date=date(2026, 2, 14), merchant="PayPal",
            old_account="Expenses:Office-Supplies",
            new_account="Expenses:Entertainment:Recreation", amount=25.00,
        )
        assert "Recategorized in Monarch" in result
        assert "Expenses:Entertainment:Recreation  25.00 USD" in result
        assert "Expenses:Office-Supplies  -25.00 USD" in result


class TestConfigParsing:
    def test_parse_monarch_config(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nemail = "test@example.com"\n\n'
            '[monarch.sync]\nlookback_days = 60\n\n'
            '[monarch.accounts]\n"Chase" = "Assets:Bank:Chase"\n\n'
            '[monarch.categories]\n"Custom" = "Expenses:Custom"\n\n'
            '[monarch.tags]\ninclude = ["Business"]\n'
        )
        config = parse_monarch_config(config_file)
        assert config.credentials.email == "test@example.com"
        assert config.sync.lookback_days == 60
        assert config.accounts["Chase"] == "Assets:Bank:Chase"
        assert config.categories["Custom"] == "Expenses:Custom"
        assert config.tags.include == ["Business"]

    def test_parse_monarch_config_with_secrets_overlay(self, tmp_path):
        """Secrets file credentials override monarch config credentials."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nemail = "test@example.com"\n\n'
            '[monarch.sync]\nlookback_days = 60\n'
        )
        secrets = {"monarch": {"session_token": "secret-token-123"}}
        config = parse_monarch_config(config_file, secrets=secrets)
        assert config.credentials.session_token == "secret-token-123"
        # email from config file is still present
        assert config.credentials.email == "test@example.com"

    def test_parse_monarch_config_secrets_override_config_credentials(self, tmp_path):
        """Secrets file takes precedence over config file for the same field."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_token = "old-token"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        secrets = {"monarch": {"session_token": "new-secret-token"}}
        config = parse_monarch_config(config_file, secrets=secrets)
        assert config.credentials.session_token == "new-secret-token"

    def test_parse_monarch_config_no_secrets(self, tmp_path):
        """Without secrets, behavior is unchanged."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_token = "inline-token"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        config = parse_monarch_config(config_file, secrets=None)
        assert config.credentials.session_token == "inline-token"

    def test_parse_monarch_config_empty_secrets(self, tmp_path):
        """Empty secrets dict doesn't break anything."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text('[monarch]\nemail = "test@example.com"\n')
        config = parse_monarch_config(config_file, secrets={})
        assert config.credentials.email == "test@example.com"
        assert config.credentials.session_token is None


class TestProfileConfigParsing:
    def test_parse_profiles(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\nlookback_days = 60\ndefault_account = "Expenses:Default"\n\n'
            '[monarch.accounts]\n"Chase" = "Assets:Bank:Chase"\n\n'
            '[monarch.profiles.business]\n'
            'ledger = "business"\n'
            'default_account = "Expenses:Business:Uncategorized"\n'
            'recategorize_account = "Expenses:Business:Personal"\n\n'
            '[monarch.profiles.business.tags]\n'
            'include = ["business"]\n\n'
            '[monarch.profiles.business.accounts]\n'
            '"Chase" = "Assets:Business:Chase"\n\n'
            '[monarch.profiles.business.categories]\n'
            '"Food & Drink" = "Expenses:Business:Meals"\n\n'
            '[monarch.profiles.personal]\n'
            'ledger = "personal"\n'
            'default_account = "Expenses:Personal:Uncategorized"\n\n'
            '[monarch.profiles.personal.tags]\n'
            'exclude = ["business"]\n'
        )
        config = parse_monarch_config(config_file)
        assert len(config.profiles) == 2

        biz = next(p for p in config.profiles if p.name == "business")
        assert biz.ledger == "business"
        assert biz.sync.default_account == "Expenses:Business:Uncategorized"
        assert biz.sync.recategorize_account == "Expenses:Business:Personal"
        assert biz.tags.include == ["business"]
        assert biz.accounts["Chase"] == "Assets:Business:Chase"
        assert biz.categories["Food & Drink"] == "Expenses:Business:Meals"
        # Inherits lookback_days from top level
        assert biz.sync.lookback_days == 60

        personal = next(p for p in config.profiles if p.name == "personal")
        assert personal.ledger == "personal"
        assert personal.sync.default_account == "Expenses:Personal:Uncategorized"
        assert personal.tags.exclude == ["business"]
        # No profile-level accounts, inherits top-level
        assert personal.accounts == {"Chase": "Assets:Bank:Chase"}

    def test_parse_no_profiles_backward_compat(self, tmp_path):
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nemail = "test@example.com"\n\n'
            '[monarch.sync]\nlookback_days = 30\n'
        )
        config = parse_monarch_config(config_file)
        assert config.profiles == []

    def test_profile_inherits_top_level_sync(self, tmp_path):
        """Profile without explicit sync settings inherits from top-level."""
        config_file = tmp_path / "monarch.toml"
        config_file.write_text(
            '[monarch]\nsession_token = "tok"\n\n'
            '[monarch.sync]\n'
            'lookback_days = 45\n'
            'default_account = "Expenses:TopLevel"\n'
            'recategorize_account = "Expenses:TopRecat"\n\n'
            '[monarch.profiles.minimal]\n'
            'ledger = "main"\n'
        )
        config = parse_monarch_config(config_file)
        assert len(config.profiles) == 1
        p = config.profiles[0]
        assert p.sync.lookback_days == 45
        assert p.sync.default_account == "Expenses:TopLevel"
        assert p.sync.recategorize_account == "Expenses:TopRecat"


class TestSyncAllProfiles:
    def test_no_profiles_uses_default_ledger(self, tmp_path):
        """Without profiles, syncs to the default (first) ledger."""
        ledger = tmp_path / "main.beancount"
        ledger.write_text("")
        config = MonarchConfig(
            credentials=MonarchCredentials(session_token="tok"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
        )
        ledgers = [{"name": "main", "path": ledger}]

        with patch("money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers, dry_run=True)

        assert result["status"] == "ok"
        assert "profiles" not in result  # no profiles = single sync result

    def test_multiple_profiles(self, tmp_path):
        """Syncs each profile to its target ledger."""
        biz_ledger = tmp_path / "business.beancount"
        biz_ledger.write_text("")
        personal_ledger = tmp_path / "personal.beancount"
        personal_ledger.write_text("")

        config = MonarchConfig(
            credentials=MonarchCredentials(session_token="tok"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="business", ledger="business",
                    sync=MonarchSyncSettings(default_account="Expenses:Biz"),
                    accounts={"Chase": "Assets:Biz:Chase"},
                    categories={}, tags=MonarchTagFilters(include=["business"]),
                ),
                MonarchProfile(
                    name="personal", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Expenses:Personal"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(exclude=["business"]),
                ),
            ],
        )
        ledgers = [
            {"name": "business", "path": biz_ledger},
            {"name": "personal", "path": personal_ledger},
        ]

        with patch("money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers, dry_run=True)

        assert result["status"] == "ok"
        assert len(result["profiles"]) == 2
        names = [p["name"] for p in result["profiles"]]
        assert "business" in names
        assert "personal" in names
        # API fetched only once
        mock_fetch.assert_called_once()

    def test_profile_routes_transactions_by_tags(self, tmp_path):
        """Transactions are routed to profiles based on tag filters."""
        biz_ledger = tmp_path / "business.beancount"
        biz_ledger.write_text("")
        personal_ledger = tmp_path / "personal.beancount"
        personal_ledger.write_text("")

        config = MonarchConfig(
            credentials=MonarchCredentials(session_token="tok"),
            sync=MonarchSyncSettings(lookback_days=30),
            accounts={}, categories={}, tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="business", ledger="business",
                    sync=MonarchSyncSettings(default_account="Expenses:Biz"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(include=["business"]),
                ),
                MonarchProfile(
                    name="personal", ledger="personal",
                    sync=MonarchSyncSettings(default_account="Expenses:Personal"),
                    accounts={}, categories={},
                    tags=MonarchTagFilters(exclude=["business"]),
                ),
            ],
        )
        ledgers = [
            {"name": "business", "path": biz_ledger},
            {"name": "personal", "path": personal_ledger},
        ]

        mock_txns = [
            {
                "id": "txn-biz",
                "date": "2026-01-15",
                "merchant": {"name": "Office Store"},
                "category": {"name": "Shopping"},
                "account": {"displayName": "Chase"},
                "amount": -50.0,
                "notes": "",
                "tags": [{"name": "business"}],
            },
            {
                "id": "txn-personal",
                "date": "2026-01-16",
                "merchant": {"name": "Grocery"},
                "category": {"name": "Groceries"},
                "account": {"displayName": "Chase"},
                "amount": -30.0,
                "notes": "",
                "tags": [],
            },
        ]

        with patch("money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = mock_txns
            result = sync_all_profiles(config, ledgers, dry_run=True)

        biz_result = next(p for p in result["profiles"] if p["name"] == "business")
        personal_result = next(p for p in result["profiles"] if p["name"] == "personal")
        assert biz_result["transaction_count"] == 1
        assert personal_result["transaction_count"] == 1

    def test_profile_ledger_not_found(self, tmp_path):
        """Error when a profile references a non-existent ledger."""
        config = MonarchConfig(
            credentials=MonarchCredentials(session_token="tok"),
            sync=MonarchSyncSettings(), accounts={}, categories={},
            tags=MonarchTagFilters(),
            profiles=[
                MonarchProfile(
                    name="missing", ledger="nonexistent",
                    sync=MonarchSyncSettings(), accounts={}, categories={},
                    tags=MonarchTagFilters(),
                ),
            ],
        )
        ledgers = [{"name": "main", "path": tmp_path / "main.beancount"}]

        with patch("money.core.transactions.fetch_monarch_transactions") as mock_fetch:
            mock_fetch.return_value = []
            result = sync_all_profiles(config, ledgers)

        # Should report error for the missing ledger profile
        assert result["profiles"][0]["status"] == "error"


class TestImportCSV:
    def test_import_creates_staging_and_appends(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text("2026-01-01 open Assets:Bank:Checking USD\n")

        csv_file = tmp_path / "export.csv"
        csv_file.write_text(
            "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner\n"
            "2026-01-15,Whole Foods,Groceries,Chase,WHOLE FOODS,,-85.50,,Stefan\n"
        )

        result = import_csv(ledger_file, csv_file, "Assets:Bank:Checking")

        assert result["status"] == "ok"
        assert result["transaction_count"] == 1
        assert "staging_file" in result

        staging = Path(result["staging_file"])
        assert staging.exists()
        assert "Whole Foods" in staging.read_text()

        assert "Whole Foods" in ledger_file.read_text()

        backups_dir = ledger_dir / "backups"
        assert backups_dir.exists()

    def test_import_file_not_found(self, tmp_path):
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        result = import_csv(ledger_file, tmp_path / "missing.csv", "Assets:Bank")
        assert result["status"] == "error"


class TestAddTransaction:
    def test_success(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')

        with patch("money.core.ledger.run_bean_check") as mock_check:
            mock_check.return_value = (True, [])
            result = add_transaction(
                ledger_file, date(2026, 2, 4), "Test Store", "Test purchase",
                "Expenses:Food:Groceries", "Assets:Bank:Checking", 25.00,
            )

        assert result["status"] == "ok"
        assert result["payee"] == "Test Store"
        assert result["amount"] == 25.00

        txn_file = ledger_dir / "transactions" / "2026.beancount"
        assert txn_file.exists()
        assert "Test Store" in txn_file.read_text()

    def test_negative_amount(self, tmp_path):
        ledger_file = tmp_path / "main.beancount"
        ledger_file.write_text("")
        result = add_transaction(
            ledger_file, date(2026, 2, 4), "Store", "Purchase",
            "Expenses:Food", "Assets:Bank", -10.00,
        )
        assert result["status"] == "error"
        assert "Amount must be positive" in result["error"]

    def test_validation_failure(self, tmp_path):
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        ledger_file = ledger_dir / "main.beancount"
        ledger_file.write_text('include "transactions/*.beancount"\n')

        with patch("money.core.ledger.run_bean_check") as mock_check:
            mock_check.return_value = (False, ["Invalid account"])
            result = add_transaction(
                ledger_file, date(2026, 2, 4), "Store", "Purchase",
                "Expenses:Bad", "Assets:Bank", 10.00,
            )

        assert result["status"] == "error"
        assert "validation failed" in result["error"]


class TestLedgerFileOps:
    def test_backup_ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("content")
        result = backup_ledger(ledger)
        assert result is not None
        assert result.exists()
        backups = list((tmp_path / "backups").glob("main.beancount.*"))
        assert len(backups) == 1

    def test_backup_nonexistent(self, tmp_path):
        result = backup_ledger(tmp_path / "missing.beancount")
        assert result is None

    def test_append_to_ledger(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("initial content\n")
        append_to_ledger(ledger, ["entry1", "entry2"])
        content = ledger.read_text()
        assert "entry1" in content
        assert "entry2" in content

    def test_append_empty_list(self, tmp_path):
        ledger = tmp_path / "main.beancount"
        ledger.write_text("initial content\n")
        append_to_ledger(ledger, [])
        assert ledger.read_text() == "initial content\n"
