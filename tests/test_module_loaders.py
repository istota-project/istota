"""Tests for the feeds + money module loader gates.

Both loaders gate on ``Config.is_module_enabled(user_id, <module>)`` after
the modules / connected services refactor — they no longer require a
matching ``[[resources]]`` entry. These tests guard the workspace-mode
resolution path.
"""

from __future__ import annotations

from pathlib import Path

from istota.config import Config, UserConfig


def _config(tmp_path: Path, *, users: dict[str, UserConfig]) -> Config:
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        workspace_path=mount,
        users=users,
        bot_name="Istota",
        db_path=tmp_path / "no.db",  # not exercised — keeps best-effort paths quiet
    )


# ---- feeds ---------------------------------------------------------------


class TestFeedsLoader:
    def test_resolve_for_user_with_module_enabled(self, tmp_path):
        from istota.feeds import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        ctx = resolve_for_user("alice", cfg)
        # Workspace path comes from get_user_bot_path. Just check that the
        # synthesized context points under the mount.
        assert str(ctx.data_dir).startswith(str(tmp_path / "mount"))


# ---- money ---------------------------------------------------------------


class TestMoneyLoader:
    def test_resolve_for_user_with_module_enabled(self, tmp_path):
        from istota.money import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        ctx = resolve_for_user("alice", cfg)
        assert str(ctx.data_dir).startswith(str(tmp_path / "mount"))

    def test_invoice_files_walk_from_the_user_root(self, tmp_path):
        """ISSUE-775: the context carries the user root, and the pinned walk
        reaches ``data_dir`` from it although ``data_dir`` is spelled resolved."""
        from istota.money import resolve_for_user
        from istota.money.invoice_files import open_output_tree

        cfg = _config(tmp_path, users={"alice": UserConfig(display_name="Alice")})
        ctx = resolve_for_user("alice", cfg)
        assert ctx.user_root == cfg.workspace_root("alice")
        year = ctx.data_dir / "invoices" / "generated" / "2026"
        year.mkdir(parents=True)
        (year / "Invoice-000007-01_02_2026.pdf").write_bytes(b"%PDF")

        with open_output_tree(ctx.user_root, ctx.data_dir, None) as tree:
            assert tree is not None
            pdf = tree.find(7)
            assert pdf is not None and pdf.name == "Invoice-000007-01_02_2026.pdf"

    def test_autoclass_lookup_gate_reaches_the_user_context(self, tmp_path):
        """`[money] autoclass_lookup` is a security control — it keeps held
        symbols off a third-party wire — and the UserContext hop is its only
        production wiring, so it needs a test that fails if the hop is
        removed."""
        from istota.config import MoneyModuleConfig
        from istota.money import resolve_for_user

        cfg = _config(tmp_path, users={"alice": UserConfig()})
        assert resolve_for_user("alice", cfg).autoclass_lookup is True

        cfg.money = MoneyModuleConfig(autoclass_lookup=False)
        assert resolve_for_user("alice", cfg).autoclass_lookup is False

    def test_activate_user_propagates_the_lookup_gate(self, tmp_path):
        """The CLI reads the gate off the activated Context, not the
        UserContext, so the copy in activate_user is the second half of the
        same wire."""
        from istota.money.cli import Context, UserContext

        ctx = Context()
        ctx.users["alice"] = UserContext(
            data_dir=tmp_path, ledgers=[], autoclass_lookup=False,
        )
        ctx.activate_user("alice")
        assert ctx.autoclass_lookup is False
