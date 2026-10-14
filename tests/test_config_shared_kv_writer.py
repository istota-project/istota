"""Tests for Config.is_shared_kv_writer — the fail-closed shared-write gate.

Pins the deliberate asymmetry vs is_admin so a future refactor can't collapse
the two.
"""

from istota.config import Config


class TestSharedKvWriter:
    def test_empty_allowlist_authorizes_nobody(self):
        config = Config(admin_users=set())
        # The fail-closed assertion, contrasted with is_admin's single-user fallback.
        assert config.is_shared_kv_writer("alice") is False
        assert config.is_admin("alice") is True

    def test_member_authorized(self):
        config = Config(admin_users={"alice", "bob"})
        assert config.is_shared_kv_writer("alice") is True
        assert config.is_shared_kv_writer("bob") is True

    def test_non_member_denied(self):
        config = Config(admin_users={"alice"})
        assert config.is_shared_kv_writer("mallory") is False

    def test_asymmetry_with_is_admin_on_populated_allowlist(self):
        # On a populated allowlist both agree for members and non-members.
        config = Config(admin_users={"alice"})
        assert config.is_shared_kv_writer("alice") == config.is_admin("alice")
        assert config.is_shared_kv_writer("mallory") == config.is_admin("mallory")


class TestNoAuthHasNoExemption:
    """The standalone shape's exemption is gone with the shape. A checkout
    running `istota serve` with `[web] auth = "none"` is decided by its admins
    file like every other install."""

    def test_the_sole_no_auth_user_is_not_a_writer_without_an_admins_file(self, make_user_config):
        from istota.config import WebConfig

        config = Config(web=WebConfig(auth="none"), users={"alice": make_user_config()})
        assert config.local_user_id == "alice"
        assert config.is_shared_kv_writer("alice") is False

    def test_an_admins_file_still_decides_with_auth_off(self, make_user_config):
        from istota.config import WebConfig

        config = Config(
            web=WebConfig(auth="none"),
            users={"alice": make_user_config()},
            admin_users={"alice"},
        )
        assert config.is_shared_kv_writer("alice") is True
        assert config.is_shared_kv_writer("mallory") is False
