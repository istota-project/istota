"""The outbound email approval gate.

The predicate keys on the recipient. That is the whole design, and the reason it
is worth a dedicated test file is the shape of the failure it replaces: an
earlier attempt ("Layer A") built its allowlist out of observed correspondence,
which meant one inbound message from a stranger permanently authorized mailing
them back. `TestLayerARegressionGuard` is the standing test against that.
"""

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    UserConfig,
)
from istota.outbound_policy import (
    HOLD_ALL_MODE,
    HOLD_UNTRUSTED,
    effective_policy,
    recipients_require_hold,
)

OWN = "alice@example.com"
CONFIG_PATTERN_MATCH = "colleague@partner.example.org"
DB_TRUSTED = "friend@example.net"
UNKNOWN = "stranger@example.invalid"


def _config(tmp_path, *, floor="untrusted", user_setting=""):
    return Config(
        db_path=tmp_path / "test.db",
        email=EmailConfig(enabled=True, outbound_approval_floor=floor),
        users={
            "alice": UserConfig(
                display_name="Alice",
                email_addresses=[OWN],
                trusted_email_senders=["*@partner.example.org"],
                outbound_approval=user_setting,
            ),
        },
    )


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path / "test.db")
    with db.get_db(tmp_path / "test.db") as c:
        db.add_trusted_sender(c, "alice", DB_TRUSTED)
        yield c


# ---------------------------------------------------------------------------
# 1. effective_policy — the operator sets a floor, the user may only tighten
# ---------------------------------------------------------------------------


class TestEffectivePolicy:
    @pytest.mark.parametrize(
        ("floor", "user_setting", "expected"),
        [
            # A user may not loosen below the floor.
            ("untrusted", "off", "untrusted"),
            ("all", "off", "all"),
            ("all", "untrusted", "all"),
            # A user may tighten past it.
            ("off", "all", "all"),
            ("off", "untrusted", "untrusted"),
            ("untrusted", "all", "all"),
            # Agreement is a no-op either way.
            ("untrusted", "untrusted", "untrusted"),
            ("off", "off", "off"),
            # Unset resolves to the floor — this is what makes raising the
            # floor reach every user who never touched the setting.
            ("untrusted", "", "untrusted"),
            ("all", "", "all"),
            ("off", "", "off"),
        ],
    )
    def test_ordering(self, tmp_path, floor, user_setting, expected):
        config = _config(tmp_path, floor=floor, user_setting=user_setting)
        assert effective_policy(config, "alice") == expected

    def test_unknown_user_resolves_to_the_floor(self, tmp_path):
        config = _config(tmp_path, floor="untrusted")
        assert effective_policy(config, "nobody") == "untrusted"

    def test_a_garbage_user_value_is_treated_as_unset(self, tmp_path, caplog):
        """Hand-edited DB row. Tightening toward the floor is the safe
        direction; honouring the garbage or falling to 'off' is not."""
        config = _config(tmp_path, floor="untrusted", user_setting="banana")
        assert effective_policy(config, "alice") == "untrusted"
        assert "banana" in caplog.text

    def test_a_garbage_floor_falls_back_to_untrusted_not_off(self, tmp_path):
        """Config load rejects this outright, so reaching it means a
        hand-built Config. It must not fail open."""
        config = _config(tmp_path, floor="nonsense")
        assert effective_policy(config, "alice") == "untrusted"


# ---------------------------------------------------------------------------
# 2. recipients_require_hold — the truth table
# ---------------------------------------------------------------------------


class TestRecipientTruthTable:
    @pytest.mark.parametrize(
        ("policy", "recipient", "expected"),
        [
            # off — nothing is ever held.
            ("off", OWN, None),
            ("off", CONFIG_PATTERN_MATCH, None),
            ("off", DB_TRUSTED, None),
            ("off", UNKNOWN, None),
            # untrusted — all three explicit-authorization sources clear.
            ("untrusted", OWN, None),
            ("untrusted", CONFIG_PATTERN_MATCH, None),
            ("untrusted", DB_TRUSTED, None),
            ("untrusted", UNKNOWN, HOLD_UNTRUSTED),
            # all — only the user's own addresses clear.
            ("all", OWN, None),
            ("all", CONFIG_PATTERN_MATCH, HOLD_ALL_MODE),
            ("all", DB_TRUSTED, HOLD_ALL_MODE),
            ("all", UNKNOWN, HOLD_ALL_MODE),
        ],
    )
    def test_single_recipient(self, tmp_path, conn, policy, recipient, expected):
        config = _config(tmp_path, floor=policy)
        assert recipients_require_hold(
            config, conn, "alice", [recipient],
        ) == expected

    def test_case_is_not_a_bypass(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [DB_TRUSTED.upper()],
        ) is None
        assert recipients_require_hold(
            config, conn, "alice", [UNKNOWN.upper()],
        ) == HOLD_UNTRUSTED

    def test_one_untrusted_recipient_holds_the_whole_message(self, tmp_path, conn):
        """Never send to the trusted subset. A partial send delivers a message
        the user never approved under a subject implying everyone got it."""
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [OWN, DB_TRUSTED, UNKNOWN],
        ) == HOLD_UNTRUSTED

    def test_all_trusted_recipients_send(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [OWN, DB_TRUSTED, CONFIG_PATTERN_MATCH],
        ) is None

    def test_all_mode_lets_a_briefing_to_yourself_through(self, tmp_path, conn):
        """Own addresses are exempt in every mode — otherwise every briefing
        would need approving."""
        config = _config(tmp_path, floor="all")
        assert recipients_require_hold(config, conn, "alice", [OWN]) is None

    def test_an_empty_recipient_list_holds(self, tmp_path, conn):
        """'Nothing to check' must not read as 'everything checked out'."""
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(config, conn, "alice", []) == HOLD_UNTRUSTED
        assert recipients_require_hold(config, conn, "alice", ["", "  "]) == HOLD_UNTRUSTED

    def test_an_unknown_user_holds_everything_above_off(self, tmp_path, conn):
        """No UserConfig means no own addresses and no trust patterns, so
        nothing can clear. Fail closed."""
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "nobody", [OWN],
        ) == HOLD_UNTRUSTED

    def test_a_missing_connection_refuses_to_answer_at_all(self, tmp_path):
        """The runtime trusted-sender table is one of the three authorization
        sources. Without it the predicate cannot tell a trusted correspondent
        from a stranger — and the two remaining branches would still clear the
        user's own address, so answering at all is a fail-open on exactly the
        caller whose DB was unavailable. The skill turns this into a refusal."""
        config = _config(tmp_path, floor="untrusted")
        with pytest.raises(ValueError):
            recipients_require_hold(config, None, "alice", [OWN])
        with pytest.raises(ValueError):
            recipients_require_hold(config, None, "alice", [DB_TRUSTED])


# ---------------------------------------------------------------------------
# 2b. The gate must key on the same expansion SMTP will use
# ---------------------------------------------------------------------------


class TestRecipientExpansion:
    """One entry may carry several addresses, and the trust check is `fnmatch`.

    Handing the raw comma-joined string to a canonical `*@domain` pattern lets
    the `*` swallow an untrusted address sitting in front of a trusted one, so
    the gate clears a message that `_recipients` then expands into two envelope
    recipients. The predicate expands first for exactly this reason.
    """

    def test_a_trusted_address_cannot_smuggle_an_untrusted_one(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        smuggled = f"{UNKNOWN}, {CONFIG_PATTERN_MATCH}"
        # The bare fnmatch the gate used to reach with this string.
        from fnmatch import fnmatch
        assert fnmatch(smuggled, "*@partner.example.org")
        # The gate is not fooled.
        assert recipients_require_hold(
            config, conn, "alice", [smuggled],
        ) == HOLD_UNTRUSTED

    def test_the_display_name_form_is_expanded_too(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice",
            [f"Evil <{UNKNOWN}>, {CONFIG_PATTERN_MATCH}"],
        ) == HOLD_UNTRUSTED

    def test_a_display_name_around_a_trusted_address_still_sends(
        self, tmp_path, conn,
    ):
        """Fail-closed must not become fail-annoying: a `Name <addr>` form is
        an ordinary way to write a recipient and resolves to the address."""
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [f"Alice <{OWN}>"],
        ) is None

    def test_a_multi_address_entry_of_trusted_addresses_sends(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [f"{OWN}, {DB_TRUSTED}"],
        ) is None

    @pytest.mark.parametrize(
        "entry", [None, 42, {"email": "x@y.test"}, ["a@b.test"], b"a@b.test"],
    )
    def test_a_non_string_entry_holds_rather_than_raising(
        self, tmp_path, conn, entry,
    ):
        """A dropped entry would be worse than a held one — the caller still
        sends it, so filtering it out makes 'checked' and 'sent' differ. And
        raising leaves the outcome to however the caller happens to catch."""
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [OWN, entry],
        ) == HOLD_UNTRUSTED

    def test_a_bare_token_with_no_at_sign_holds(self, tmp_path, conn):
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", ["garbage"],
        ) == HOLD_UNTRUSTED

    def test_a_catch_all_trust_pattern_is_warned_about(self, tmp_path, conn, caplog):
        """`trusted_email_senders` was inbound-only before this feature, where
        a broad pattern only means 'stop asking me'. It now authorizes outbound
        too, so a catch-all turns the gate off while the floor still reads
        'untrusted' everywhere. Not a refusal — the entry predates the meaning
        — but it must not be silent."""
        import istota.outbound_policy as mod

        mod._warned_catch_all.clear()
        config = _config(tmp_path, floor="untrusted")
        config.users["alice"].trusted_email_senders = ["*"]

        assert recipients_require_hold(config, conn, "alice", [UNKNOWN]) is None
        assert "matches every address" in caplog.text

        # Once per (user, pattern), not once per send.
        caplog.clear()
        assert recipients_require_hold(config, conn, "alice", [UNKNOWN]) is None
        assert "matches every address" not in caplog.text

    @pytest.mark.parametrize(
        ("pattern", "catch_all"),
        [
            ("*", True),
            ("*@*", True),
            ("*@?", True),
            ("*@", True),
            ("*@partner.example.org", False),
            ("colleague@partner.example.org", False),
            ("*@*.example.org", False),
        ],
    )
    def test_catch_all_detection(self, pattern, catch_all):
        from istota.outbound_policy import _pattern_matches_everything

        assert _pattern_matches_everything(pattern) is catch_all


# ---------------------------------------------------------------------------
# 3. The Layer A regression guard
# ---------------------------------------------------------------------------


class TestLayerARegressionGuard:
    """Layer A (d7aba2d + f36c6c2, reverted in 67b5200 + ff381d6) derived its
    allowlist from `sent_emails.to_addr` and `processed_emails.sender_email`.

    Both are records of correspondence, not of authorization. `processed_emails`
    in particular means "we received mail from this address", so one message
    from a stranger allowlisted them for outbound — inverting the gate exactly
    where it was needed most. These tests exist to fail loudly if anyone
    reintroduces correspondence-derived trust, including as a "we already
    replied to them once" shortcut.
    """

    def test_prior_inbound_from_a_stranger_does_not_authorize_mailing_them(
        self, tmp_path, conn,
    ):
        db.mark_email_processed(
            conn, email_id="1", sender_email=UNKNOWN,
            subject="Invite", user_id="alice", routing_method="sender_match",
        )
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [UNKNOWN],
        ) == HOLD_UNTRUSTED

    def test_prior_outbound_to_a_stranger_does_not_authorize_mailing_them_again(
        self, tmp_path, conn,
    ):
        db.record_sent_email(
            conn, user_id="alice", message_id="<sent-1@example.com>",
            to_addr=UNKNOWN, subject="Re: Invite",
        )
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [UNKNOWN],
        ) == HOLD_UNTRUSTED

    def test_a_full_prior_exchange_still_does_not_authorize(self, tmp_path, conn):
        """The ISSUE-245 shape: they wrote, we replied, they wrote again. Every
        later message in that thread is still a new decision."""
        db.mark_email_processed(
            conn, email_id="1", sender_email=UNKNOWN,
            subject="Invite", user_id="alice", routing_method="thread_match",
        )
        db.record_sent_email(
            conn, user_id="alice", message_id="<sent-1@example.com>",
            to_addr=UNKNOWN, subject="Re: Invite",
        )
        db.mark_email_processed(
            conn, email_id="2", sender_email=UNKNOWN,
            subject="Re: Invite", user_id="alice", routing_method="thread_match",
        )
        config = _config(tmp_path, floor="untrusted")
        assert recipients_require_hold(
            config, conn, "alice", [UNKNOWN],
        ) == HOLD_UNTRUSTED


# ---------------------------------------------------------------------------
# 4. Config load validation
# ---------------------------------------------------------------------------


class TestFloorValidation:
    def test_a_bad_floor_fails_the_load(self, tmp_path):
        """Not a warn-and-fall-back: every fallback value is wrong in one
        direction, so a typo in a security floor stops the process."""
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            '[email]\nenabled = true\noutbound_approval_floor = "untrsuted"\n'
        )
        with pytest.raises(ValueError) as excinfo:
            load_config(path)
        assert "untrsuted" in str(excinfo.value)
        assert "untrusted" in str(excinfo.value)

    @pytest.mark.parametrize("value", ["off", "untrusted", "all"])
    def test_each_valid_floor_loads(self, tmp_path, value):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            f'[email]\nenabled = true\noutbound_approval_floor = "{value}"\n'
        )
        assert load_config(path).email.outbound_approval_floor == value

    def test_the_default_floor_is_untrusted(self, tmp_path):
        """A fresh install ships with the gate on. An install that ships with
        it off has no gate at all."""
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text("[email]\nenabled = true\n")
        assert load_config(path).email.outbound_approval_floor == "untrusted"


# ---------------------------------------------------------------------------
# 5. The settings round-trip through user_profiles
# ---------------------------------------------------------------------------


class TestProfileRoundTrip:
    def test_both_columns_round_trip(self, tmp_path):
        from istota import user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice")

        user_profiles.update_profile(
            db_path, "alice",
            outbound_approval="all", external_turn_display="hidden",
        )
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == "all"
        assert profile.external_turn_display == "hidden"

    def test_defaults_are_unset_and_collapsed(self, tmp_path):
        from istota import user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        profile = user_profiles.ensure_profile(db_path, "alice")
        assert profile.outbound_approval == ""
        assert profile.external_turn_display == "collapsed"

    def test_clearing_outbound_approval_returns_to_following_the_floor(
        self, tmp_path,
    ):
        """'' is a real value, not a missing one — it means "follow the
        operator". A user who tightened and then cleared must go back to
        tracking the floor rather than being pinned at their old choice."""
        from istota import user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(db_path, "alice", outbound_approval="all")
        user_profiles.update_profile(db_path, "alice", outbound_approval="")

        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.outbound_approval == ""

        config = _config(tmp_path, floor="untrusted")
        user_profiles.merge_into_user_config(profile, config.users["alice"])
        assert effective_policy(config, "alice") == "untrusted"

    def test_a_profile_row_drives_the_effective_policy(self, tmp_path):
        from istota import user_profiles

        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.update_profile(db_path, "alice", outbound_approval="all")

        config = _config(tmp_path, floor="untrusted")
        profile = user_profiles.get_profile(db_path, "alice")
        user_profiles.merge_into_user_config(profile, config.users["alice"])
        assert effective_policy(config, "alice") == "all"
