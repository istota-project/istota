"""Stage 5: `istota usage` — mostly the rendering rules.

Those are where the cost decision actually lands. Everything upstream stores
`cost_usd` and `cost_basis` on every row; the surface is what decides whether a
currency figure appears, and getting that wrong means a dashboard quietly
inventing an invoice.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from istota import cli, db
from istota.usage import BrainUsage, ModelUsage

NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _usage(**kw):
    base = dict(
        billed_input_tokens=100,
        output_tokens=50,
        cache_read_tokens=1000,
        cache_write_tokens=200,
        cost_usd=0.25,
        cost_basis="api",
        totals_source="model_usage",
        has_totals=True,
        turns=1,
        model_requests=2,
        initial_context_tokens=10000,
        peak_context_tokens=12000,
        context_window=200000,
        model="model-a",
    )
    base.update(kw)
    u = BrainUsage(**base)
    u.models = [
        ModelUsage(
            model=base["model"],
            billed_input_tokens=base["billed_input_tokens"],
            output_tokens=base["output_tokens"],
            cache_read_tokens=base["cache_read_tokens"],
            cache_write_tokens=base["cache_write_tokens"],
            cost_usd=base["cost_usd"],
            context_window=base["context_window"] or 0,
        )
    ]
    return u


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Three rows spanning all three cost bases, plus a config the CLI loads."""
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)

    from istota.config import Config

    config = Config()
    config.db_path = dbp

    monkeypatch.setattr(cli, "load_config", lambda _p=None: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)

    rows = [
        # api: real money, and deliberately NOT the largest by tokens.
        dict(user_id="alice", cost_basis="api", cost_usd=9.0,
             billed_input_tokens=10, output_tokens=5,
             cache_read_tokens=10, cache_write_tokens=5, model="model-a"),
        # subscription: a list-price equivalent, and the largest by tokens.
        dict(user_id="bob", cost_basis="subscription", cost_usd=99.0,
             billed_input_tokens=5000, output_tokens=100,
             cache_read_tokens=9000, cache_write_tokens=100, model="model-b"),
        # estimated: a catalog figure, which prices an unknown model at zero.
        dict(user_id="carol", cost_basis="estimated", cost_usd=0.0,
             billed_input_tokens=200, output_tokens=20,
             cache_read_tokens=300, cache_write_tokens=20, model="model-c"),
    ]
    with db.get_db(dbp) as conn:
        for row in rows:
            user = row.pop("user_id")
            rid = db.insert_task_usage(
                conn, usage=_usage(**row), user_id=user,
                brain_kind="claude_code", origin="task", success=True,
                model=row["model"],
            )
            conn.execute(
                "UPDATE task_usage SET created_at = ? WHERE id = ?",
                (_iso(NOW - timedelta(days=1)), rid),
            )
    return config


def _totals_block(out: str) -> str:
    """The first of the two rendered blocks (billed/cache/output/cost)."""
    return out.split("\n\n")[0]


def _context_block(out: str) -> str:
    """The second block (measured count, avg initial, avg peak, peak %)."""
    return out.split("\n\n")[1]


def _args(**kw):
    base = dict(
        config=None, days=30, since=None, until=None, user=None, brain=None,
        source=None, model=None, origin=None, by=None, json=False, verbose=False,
    )
    base.update(kw)
    return type("Args", (), base)()


def _run(capsys, **kw):
    code = cli.cmd_usage(_args(**kw))
    return code, capsys.readouterr().out


class TestCostRendering:
    def test_only_the_real_money_row_renders_as_currency(self, seeded, capsys):
        """Asserted together so a change that suppresses too much or too little
        fails. One rule: no currency unless it is money.

        The two non-money rows are asserted to be indistinguishable from each
        other: alice's spend is `api`, bob's is a subscription's list-price
        equivalent and carol's a catalog estimate, and the last two are both
        just a dash. Naming the basis beside it varied the rendering without
        varying the one thing the column reports.
        """
        _, out = _run(capsys, by="user")

        # The first block is totals; the second is context. Both have a row per
        # key, so the lookup has to name which block it means.
        totals = _totals_block(out)
        lines = {ln.split()[0]: ln for ln in totals.splitlines() if ln[:1].isalpha()}
        assert "$9.00" in lines["alice"]
        assert "$" not in lines["bob"]
        assert "subscription" not in lines["bob"]
        assert "—" in lines["bob"]
        assert "$" not in lines["carol"]
        assert "estimated" not in lines["carol"]
        assert "—" in lines["carol"]

    def test_a_subscription_only_window_still_prints_a_usable_table(
        self, seeded, capsys
    ):
        """The common case on a subscription deployment. Tokens, context and
        cache rate carry the table; ranking by cost is simply unavailable."""
        _, out = _run(capsys, by="user", user="bob")

        assert "5,000" in out
        assert "9,000" in out
        assert "—" in out
        assert "$" not in out

    def test_nothing_sums_across_cost_basis(self, seeded, capsys):
        """9.0 + 99.0 = 108. A total that lumps a plan-equivalent in with real
        spend is the misread the whole design refuses."""
        _, out = _run(capsys)

        assert "108" not in out
        assert "$99" not in out

    def test_a_group_spanning_bases_marks_rather_than_sums(self, seeded, capsys):
        """An operator switching the CLI's auth mid-window has rows of both
        kinds under one key."""
        with db.get_db(seeded.db_path) as conn:
            conn.execute("UPDATE task_usage SET user_id = 'alice'")

        _, out = _run(capsys, by="user")

        line = next(
            ln for ln in _totals_block(out).splitlines() if ln.startswith("alice")
        )
        assert "$9.00" in line
        assert "estimated" in line or "subscription" in line
        assert "108" not in line


class TestMoneyPrecision:
    def test_a_sub_cent_figure_is_not_rounded_to_a_flat_zero(self, seeded, capsys):
        """A 24h per-user cost is routinely sub-cent. At two decimals it renders
        `$0.00` — indistinguishable from a genuine zero, which is the one thing
        a cost column must not be ambiguous about."""
        with db.get_db(seeded.db_path) as conn:
            conn.execute(
                "UPDATE task_usage SET cost_usd = 0.0004 WHERE cost_basis = 'api'"
            )

        _, out = _run(capsys, by="user")

        line = next(ln for ln in _totals_block(out).splitlines() if ln.startswith("alice"))
        assert "$0.0004" in line
        assert "$0.00 " not in line

    def test_an_ordinary_figure_keeps_two_decimals(self, seeded, capsys):
        _, out = _run(capsys, by="user")

        line = next(ln for ln in _totals_block(out).splitlines() if ln.startswith("alice"))
        assert "$9.00" in line

    def test_a_genuine_zero_of_real_money_still_renders_as_currency(
        self, seeded, capsys
    ):
        with db.get_db(seeded.db_path) as conn:
            conn.execute(
                "UPDATE task_usage SET cost_usd = 0.0 WHERE cost_basis = 'api'"
            )

        _, out = _run(capsys, by="user")

        line = next(ln for ln in _totals_block(out).splitlines() if ln.startswith("alice"))
        assert "$0.00" in line


class TestExitCode:
    def test_a_failing_run_exits_non_zero_through_main(self, seeded, monkeypatch):
        """`cmd_usage` returning 1 is worth nothing if `main` throws it away —
        a script cannot see a message on stdout."""
        monkeypatch.setattr(
            "sys.argv", ["istota", "usage", "--since", "not-a-date"]
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1

    def test_a_successful_run_exits_zero(self, seeded, monkeypatch):
        monkeypatch.setattr("sys.argv", ["istota", "usage", "--json"])

        try:
            cli.main()
        except SystemExit as exc:  # pragma: no cover - only on a regression
            assert exc.code in (None, 0)


class TestJson:
    def test_json_emits_cost_and_basis_for_every_row(self, seeded, capsys):
        """The suppression is a rendering rule, not a data rule. A consumer that
        also receives `cost_basis` can apply its own; zeroing the field upstream
        would force it to re-derive what is already known."""
        _, out = _run(capsys, by="user", json=True)
        payload = json.loads(out)

        bases = {}
        for group in payload["groups"]:
            bases.update(group["cost_by_basis"])

        assert set(bases) == {"api", "subscription", "estimated"}
        assert bases["subscription"] == pytest.approx(99.0)
        assert bases["api"] == pytest.approx(9.0)

    def test_json_carries_the_window_and_the_honesty_counter(self, seeded, capsys):
        _, out = _run(capsys, json=True)
        payload = json.loads(out)

        assert payload["since"].endswith("Z")
        assert "unmeasured_tasks" in payload


class TestSorting:
    def test_the_default_sort_is_by_tokens_not_cost(self, seeded, capsys):
        """A cost-descending sort is an all-blank sort key on a subscription
        deployment. The seeded rows make the two orders disagree: alice has the
        largest cost and the smallest token count."""
        _, out = _run(capsys, by="user")

        keys = [
            ln.split()[0] for ln in _totals_block(out).splitlines()
            if ln[:1].isalpha() and ln.split()[0] in {"alice", "bob", "carol"}
        ]
        assert keys[0] == "bob"
        assert keys[-1] == "alice"

    def test_by_day_sorts_chronologically(self, seeded, capsys):
        with db.get_db(seeded.db_path) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM task_usage ORDER BY id")]
            for offset, rid in enumerate(ids):
                conn.execute(
                    "UPDATE task_usage SET created_at = ? WHERE id = ?",
                    (_iso(NOW - timedelta(days=offset + 1)), rid),
                )

        _, out = _run(capsys, by="day", json=True)
        keys = [g["key"] for g in json.loads(out)["groups"]]

        assert keys == sorted(keys)


class TestFiltersAndWindow:
    def test_until_is_expanded_to_the_following_midnight(self, seeded, capsys):
        """A bare `--until D` that excluded D would silently lose a whole day."""
        day = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")

        _, out = _run(
            capsys, since=(NOW - timedelta(days=5)).strftime("%Y-%m-%d"),
            until=day, json=True,
        )
        payload = json.loads(out)

        assert payload["groups"][0]["rows"] == 3

    def test_a_bad_date_is_rejected_rather_than_ignored(self, seeded, capsys):
        code, out = _run(capsys, since="yesterday")

        assert code == 1
        assert "YYYY-MM-DD" in out

    @pytest.mark.parametrize("days", [0, -5])
    def test_a_non_positive_days_is_rejected(self, seeded, capsys, days):
        """`--days 0` reads as "no limit" and does the opposite — it puts the
        bound at now and reports nothing, which looks like a real answer."""
        code, out = _run(capsys, days=days)

        assert code == 1
        assert "--days" in out

    def test_an_inverted_window_is_rejected(self, seeded, capsys):
        code, out = _run(capsys, since="2026-08-20", until="2026-01-01")

        assert code == 1
        assert "--until" in out

    def test_the_unmeasured_counter_uses_the_window_the_table_describes(
        self, seeded, capsys
    ):
        """The trailer says "in this window". It has to mean the same window.

        The counter reads `tasks`, whose dates are in the other format, so it
        used to be derived separately from `--days` alone — which made it
        ignore `--since` and `--until` and describe the last 30 days while the
        table above it described something else.
        """
        with db.get_db(seeded.db_path) as conn:
            old_task = db.create_task(conn, prompt="p", user_id="alice")
            new_task = db.create_task(conn, prompt="p", user_id="alice")
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ((NOW - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"), old_task),
            )
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ((NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), new_task),
            )

        # Default 30-day window sees only the recent one.
        _, out = _run(capsys, json=True)
        assert json.loads(out)["unmeasured_tasks"] == 1

        # A window reaching back past both sees both.
        _, out = _run(capsys, since="2020-01-01", json=True)
        assert json.loads(out)["unmeasured_tasks"] == 2

        # And `--until` bounds it from the other side.
        _, out = _run(
            capsys, since="2020-01-01",
            until=(NOW - timedelta(days=30)).strftime("%Y-%m-%d"), json=True,
        )
        assert json.loads(out)["unmeasured_tasks"] == 1

    @pytest.mark.parametrize(
        "flag,value,expected",
        [("user", "alice", 1), ("brain", "claude_code", 3), ("model", "model-b", 1)],
    )
    def test_filters_narrow_the_window(self, seeded, capsys, flag, value, expected):
        _, out = _run(capsys, json=True, **{flag: value})

        assert json.loads(out)["groups"][0]["rows"] == expected

    def test_an_empty_window_says_so(self, seeded, capsys):
        code, out = _run(capsys, days=1, since="2020-01-01", until="2020-01-02")

        assert code == 0
        assert "No usage recorded" in out


class TestContextBlock:
    def test_context_is_rendered_in_its_own_block(self, seeded, capsys):
        """The two groups of measures are not comparable — one sums across
        requests, the other is a first and a max — so they are never in the same
        row of numbers."""
        _, out = _run(capsys, by="user")

        assert "Billed in" in out
        assert "Avg initial" in out
        totals_at = out.index("Billed in")
        context_at = out.index("Avg initial")
        assert totals_at < context_at
        # And the two blocks are separated.
        assert "\n\n" in out

    def test_an_unmeasured_context_renders_a_placeholder_not_a_zero(
        self, seeded, capsys
    ):
        with db.get_db(seeded.db_path) as conn:
            conn.execute(
                "UPDATE task_usage SET initial_context_tokens = NULL,"
                " peak_context_tokens = NULL, context_window = NULL"
            )

        _, out = _run(capsys, by="user")

        assert "—" in out
        # The three context *values* are placeholders. `context_rows` is a count
        # of measured rows and is legitimately 0, so it is excluded here.
        for line in _context_block(out).splitlines():
            if line[:1].isalpha() and line.split()[0] in {"alice", "bob", "carol"}:
                assert line.split()[2:] == ["—", "—", "—"], line


class TestMissingTable:
    def test_a_database_without_the_table_explains_itself(
        self, tmp_path, monkeypatch, capsys
    ):
        dbp = tmp_path / "bare.db"
        with db.sqlite3.connect(dbp) as conn:
            conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")

        from istota.config import Config

        config = Config()
        config.db_path = dbp
        monkeypatch.setattr(cli, "load_config", lambda _p=None: config)

        code, out = _run(capsys)

        assert code == 1
        assert "task_usage" in out
