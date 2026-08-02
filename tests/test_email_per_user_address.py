"""Per-user plus address helper — the address a user can mail the bot at."""

import pytest

from istota.config import Config
from istota.email_support import per_user_address


def _config(*, enabled=True, bot_email="istota@bot.example.com"):
    config = Config()
    config.email.enabled = enabled
    config.email.bot_email = bot_email
    return config


class TestPerUserAddress:
    def test_builds_plus_address_from_bot_email(self):
        assert per_user_address(_config(), "alice") == "istota+alice@bot.example.com"

    def test_none_when_email_disabled(self):
        assert per_user_address(_config(enabled=False), "alice") is None

    @pytest.mark.parametrize("bot_email", ["", "not-an-address", "@nolocal.com", "nodomain@"])
    def test_none_when_bot_email_unusable(self, bot_email):
        assert per_user_address(_config(bot_email=bot_email), "alice") is None

    def test_none_without_a_user_id(self):
        assert per_user_address(_config(), "") is None

    def test_plus_in_the_local_part_is_kept(self):
        # A bot_email that already carries a tag would otherwise produce a
        # second '+', which no MTA routes back to us.
        assert per_user_address(_config(bot_email="istota+bot@x.com"), "alice") is None
