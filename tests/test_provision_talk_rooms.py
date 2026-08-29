"""ISSUE-115 — bare-metal (Ansible) deploys never provisioned the default Talk rooms.

Docker installs get `#general` / `#logs` / `#alerts` from
`docker/istota/entrypoint.sh`, which then seeds the tokens into the user's
profile. The Ansible role only forwarded `--log-channel` / `--alerts-channel`
to `istota user ensure` when the operator had already put tokens in inventory,
so out of the box `log_channel` stayed empty, `effective_log_destinations`
returned `[]`, and the execution log was off.

The fix is a shared, testable implementation — `istota.provision_rooms`, driven
by `istota nextcloud provision-rooms` — plus an Ansible task that calls it. The
tests below pin the three properties that make it safe to run on every deploy:
idempotence by participant-scoped lookup, group (not public) rooms, and seeding
that never clobbers an operator-pinned value.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from jinja2 import Environment

from istota import db, user_profiles

REPO = Path(__file__).resolve().parent.parent
TASKS_FILE = REPO / "deploy" / "ansible" / "tasks" / "main.yml"
DEFAULTS_FILE = REPO / "deploy" / "ansible" / "defaults" / "main.yml"
ENTRYPOINT = REPO / "docker" / "istota" / "entrypoint.sh"


def _fake_client(rooms=None, participants=None, created_token="newtok"):
    """A stand-in TalkClient exposing only the four OCS methods used here."""
    client = MagicMock()
    client.list_conversations = AsyncMock(return_value=list(rooms or []))
    client.get_participants = AsyncMock(
        side_effect=lambda token: list((participants or {}).get(token, []))
    )
    client.create_conversation = AsyncMock(
        return_value={"token": created_token, "name": "x"}
    )
    client.add_participant = AsyncMock(return_value={})
    client.aclose = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# Room lookup — the idempotence primitive
# ---------------------------------------------------------------------------


class TestFindRoomForUser:
    @pytest.mark.asyncio
    async def test_reuses_room_the_user_is_already_in(self):
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(
            rooms=[{"token": "tok1", "displayName": "logs"}],
            participants={"tok1": [{"actorType": "users", "actorId": "alice"}]},
        )
        assert await find_room_for_user(client, "logs", "alice") == "tok1"

    @pytest.mark.asyncio
    async def test_ignores_same_named_room_of_another_user(self):
        # The bot sits in every user's rooms, so a bare name match would hand
        # alice bob's #logs on a shared Nextcloud. Participation is the scope.
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(
            rooms=[{"token": "bobs", "displayName": "logs"}],
            participants={"bobs": [{"actorType": "users", "actorId": "bob"}]},
        )
        assert await find_room_for_user(client, "logs", "alice") is None

    @pytest.mark.asyncio
    async def test_matches_on_name_when_displayname_absent(self):
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(
            rooms=[{"token": "tok1", "name": "alerts"}],
            participants={"tok1": [{"userId": "alice"}]},
        )
        assert await find_room_for_user(client, "alerts", "alice") == "tok1"

    @pytest.mark.asyncio
    async def test_skips_group_actor_with_matching_id(self):
        # A circle/group actor whose id happens to equal the user id is not
        # the user; only `users`-type actors count.
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(
            rooms=[{"token": "tok1", "displayName": "logs"}],
            participants={"tok1": [{"actorType": "groups", "actorId": "alice"}]},
        )
        assert await find_room_for_user(client, "logs", "alice") is None


# ---------------------------------------------------------------------------
# Room creation
# ---------------------------------------------------------------------------


class TestEnsureRoom:
    @pytest.mark.asyncio
    async def test_creates_group_room_and_invites_user(self):
        from istota.provision_rooms import GROUP_ROOM_TYPE, ensure_room

        client = _fake_client(created_token="fresh")
        result = await ensure_room(client, "general", "alice")

        assert result.token == "fresh"
        assert result.created is True
        client.create_conversation.assert_awaited_once_with(
            "general", room_type=GROUP_ROOM_TYPE
        )
        client.add_participant.assert_awaited_once_with("fresh", "alice")

    @pytest.mark.asyncio
    async def test_group_room_type_is_not_public(self):
        # Talk types: 1 = one-to-one, 2 = group, 3 = public. #logs carries the
        # verbose execution log; a public room is joinable by anyone holding
        # its token, so this must stay 2.
        from istota.provision_rooms import GROUP_ROOM_TYPE

        assert GROUP_ROOM_TYPE == 2

    @pytest.mark.asyncio
    async def test_existing_room_is_not_recreated(self):
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "tok1", "displayName": "general"}],
            participants={"tok1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(client, "general", "alice")

        assert (result.token, result.created) == ("tok1", False)
        client.create_conversation.assert_not_awaited()
        client.add_participant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_failure_still_returns_the_created_room(self):
        # The room exists once create returns; losing the token because the
        # invite failed would create a duplicate on the next run.
        from istota.provision_rooms import ensure_room

        client = _fake_client(created_token="fresh")
        client.add_participant = AsyncMock(side_effect=RuntimeError("boom"))
        result = await ensure_room(client, "general", "alice")

        assert (result.token, result.created) == ("fresh", True)
        assert result.invited is False

    @pytest.mark.asyncio
    async def test_missing_token_in_create_response_raises(self):
        from istota.provision_rooms import ProvisionError, ensure_room

        client = _fake_client()
        client.create_conversation = AsyncMock(return_value={})
        with pytest.raises(ProvisionError):
            await ensure_room(client, "general", "alice")


class TestOrphanAdoption:
    """A created room whose invite failed must be reused, not duplicated."""

    @pytest.mark.asyncio
    async def test_adopts_a_bot_only_room_and_retries_the_invite(self):
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "orphan", "displayName": "logs", "type": 2}],
            participants={"orphan": [{"actorType": "users", "actorId": "bot"}]},
        )
        result = await ensure_room(client, "logs", "alice", bot_user_id="bot")

        assert (result.token, result.created, result.adopted) == ("orphan", False, True)
        assert result.invited is True
        client.create_conversation.assert_not_awaited()
        client.add_participant.assert_awaited_once_with("orphan", "alice")

    @pytest.mark.asyncio
    async def test_a_failed_invite_does_not_mint_a_room_per_run(self):
        # The bug this guards: run 1 creates `logs` and the invite fails; run 2
        # must find that room rather than create a second one, for ever.
        from istota.provision_rooms import ensure_room

        client = _fake_client(created_token="T1")
        client.add_participant = AsyncMock(side_effect=RuntimeError("no permission"))
        first = await ensure_room(client, "logs", "alice", bot_user_id="bot")
        assert (first.token, first.created, first.invited) == ("T1", True, False)

        # Second run sees the room the first one left behind.
        client.list_conversations = AsyncMock(
            return_value=[{"token": "T1", "displayName": "logs", "type": 2}]
        )
        client.get_participants = AsyncMock(
            return_value=[{"actorType": "users", "actorId": "bot"}]
        )
        client.create_conversation.reset_mock()
        second = await ensure_room(client, "logs", "alice", bot_user_id="bot")

        assert second.token == "T1"
        assert second.adopted is True
        client.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_adopts_another_users_room(self):
        # bob's #logs has bob in it, so it is not an orphan of alice's.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "bobs", "displayName": "logs", "type": 2}],
            participants={
                "bobs": [
                    {"actorType": "users", "actorId": "bot"},
                    {"actorType": "users", "actorId": "bob"},
                ]
            },
            created_token="alices",
        )
        result = await ensure_room(client, "logs", "alice", bot_user_id="bot")

        assert result.token == "alices"
        assert result.created is True

    @pytest.mark.asyncio
    async def test_adopts_nothing_without_a_bot_id(self):
        # Without knowing who the bot is, "bot-only" is unknowable — so don't
        # guess. Creating a duplicate is recoverable; stealing a room is not.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "orphan", "displayName": "logs", "type": 2}],
            participants={"orphan": [{"actorType": "users", "actorId": "bot"}]},
            created_token="fresh",
        )
        result = await ensure_room(client, "logs", "alice")
        assert result.token == "fresh"

    @pytest.mark.asyncio
    async def test_empty_participant_list_is_not_an_orphan(self):
        # More likely a failed read than a real room.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "unknown", "displayName": "logs", "type": 2}],
            participants={"unknown": []},
            created_token="fresh",
        )
        result = await ensure_room(client, "logs", "alice", bot_user_id="bot")
        assert result.token == "fresh"


class TestRealisticRoomList:
    """Drive the lookup from a payload shaped like a real Talk room list."""

    ROOMS = [
        # A one-to-one with a user whose id happens to be `logs`: Talk puts the
        # other party's id in `name`, so a bare name match would adopt it.
        {"token": "dm1", "type": 1, "name": "logs", "displayName": "Logs Person"},
        # Another user's #logs.
        {"token": "bobs", "type": 2, "name": "logs", "displayName": "logs"},
        # alice's own.
        {"token": "alices", "type": 2, "name": "logs", "displayName": "logs"},
        # A public room of the same name.
        {"token": "pub", "type": 3, "name": "logs", "displayName": "logs"},
    ]
    PARTICIPANTS = {
        "dm1": [
            {"actorType": "users", "actorId": "bot"},
            {"actorType": "users", "actorId": "alice"},
        ],
        "bobs": [{"actorType": "users", "actorId": "bob"}],
        "alices": [
            {"actorType": "users", "actorId": "bot"},
            {"actorType": "users", "actorId": "alice"},
        ],
        "pub": [{"actorType": "users", "actorId": "alice"}],
    }

    @pytest.mark.asyncio
    async def test_picks_the_group_room_not_the_one_to_one(self):
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(rooms=self.ROOMS, participants=self.PARTICIPANTS)
        assert await find_room_for_user(client, "logs", "alice") == "alices"

    @pytest.mark.asyncio
    async def test_a_guest_participant_is_not_the_user(self):
        from istota.provision_rooms import find_room_for_user

        client = _fake_client(
            rooms=[{"token": "t", "type": 2, "displayName": "logs"}],
            participants={"t": [{"actorType": "guests", "actorId": "alice"}]},
        )
        assert await find_room_for_user(client, "logs", "alice") is None


class TestProvisionRooms:
    @pytest.mark.asyncio
    async def test_fetches_the_room_list_once_for_all_names(self):
        # Three names used to mean three identical full-list GETs per user, and
        # the Ansible loop runs one process per user.
        from istota.provision_rooms import provision_rooms

        client = _fake_client()
        client.create_conversation = AsyncMock(
            side_effect=lambda name, room_type: {"token": f"tok-{name}"}
        )
        await provision_rooms(client, "alice", bot_user_id="bot")
        assert client.list_conversations.await_count == 1

    @pytest.mark.asyncio
    async def test_provisions_the_three_defaults_in_order(self):
        from istota.provision_rooms import DEFAULT_ROOMS, provision_rooms

        assert DEFAULT_ROOMS == ("general", "logs", "alerts")
        client = _fake_client()
        client.create_conversation = AsyncMock(
            side_effect=lambda name, room_type: {"token": f"tok-{name}"}
        )
        results = await provision_rooms(client, "alice")

        assert [r.name for r in results] == ["general", "logs", "alerts"]
        assert [r.token for r in results] == ["tok-general", "tok-logs", "tok-alerts"]


# ---------------------------------------------------------------------------
# Seeding the profile — the half that actually turns the execution log on
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


class TestSeedChannels:
    def test_seeds_log_and_alerts_from_room_tokens(self, db_path):
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        rooms = [
            ProvisionedRoom(name="general", token="g", created=True),
            ProvisionedRoom(name="logs", token="l", created=True),
            ProvisionedRoom(name="alerts", token="a", created=True),
        ]
        seeded, state = seed_channel_profile(db_path, "alice", rooms)

        assert seeded == {"log_channel": "l", "alerts_channel": "a"}
        assert state == "created"
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.log_channel == "l"
        assert profile.alerts_channel == "a"

    def test_never_overwrites_an_operator_pinned_channel(self, db_path):
        # The Ansible role runs `user ensure` (inventory values) before this,
        # so a pinned token must survive every redeploy.
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        user_profiles.update_profile_with_status(
            db_path, "alice", log_channel="pinned"
        )
        rooms = [
            ProvisionedRoom(name="logs", token="l", created=True),
            ProvisionedRoom(name="alerts", token="a", created=True),
        ]
        seeded, state = seed_channel_profile(db_path, "alice", rooms)

        assert "log_channel" not in seeded
        assert seeded == {"alerts_channel": "a"}
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.log_channel == "pinned"
        assert profile.alerts_channel == "a"

    def test_second_run_is_a_noop(self, db_path):
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        rooms = [
            ProvisionedRoom(name="logs", token="l", created=True),
            ProvisionedRoom(name="alerts", token="a", created=True),
        ]
        seed_channel_profile(db_path, "alice", rooms)
        seeded, state = seed_channel_profile(db_path, "alice", rooms)

        assert seeded == {}
        assert state == "noop"

    def test_room_without_a_channel_mapping_is_ignored(self, db_path):
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        seeded, _ = seed_channel_profile(
            db_path, "alice", [ProvisionedRoom(name="general", token="g", created=True)]
        )
        assert seeded == {}

    def test_does_not_refill_a_channel_the_user_cleared(self, db_path):
        # An empty log_channel is the web UI's "(off)" — the execution log is
        # opt-in (`effective_log_destinations`). A room that already existed
        # with the user in it must not re-enable it. That is the ISSUE-102
        # timezone clobber in a new place.
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        user_profiles.update_profile_with_status(db_path, "alice", log_channel="")
        rooms = [ProvisionedRoom(name="logs", token="l", created=False, invited=False)]
        seeded, state = seed_channel_profile(db_path, "alice", rooms)

        assert seeded == {}
        assert state == "noop"
        assert (user_profiles.get_profile(db_path, "alice").log_channel or "") == ""

    def test_seeds_a_room_adopted_after_a_failed_invite(self, db_path):
        # Run 1 created the room but the invite failed, so nothing was seeded.
        # Run 2 adopts it and the invite lands — that is this user's first
        # usable #logs, so it seeds.
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        rooms = [ProvisionedRoom(name="logs", token="l", created=False, adopted=True)]
        seeded, _ = seed_channel_profile(db_path, "alice", rooms)
        assert seeded == {"log_channel": "l"}

    def test_does_not_seed_a_room_the_invite_failed_for(self, db_path):
        # The bot could post there; the user could not read it. Worse than off.
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        rooms = [ProvisionedRoom(name="logs", token="l", created=True, invited=False)]
        seeded, _ = seed_channel_profile(db_path, "alice", rooms)
        assert seeded == {}

    def test_force_repoints_an_existing_channel(self, db_path):
        from istota.provision_rooms import ProvisionedRoom, seed_channel_profile

        user_profiles.update_profile_with_status(db_path, "alice", log_channel="old")
        rooms = [ProvisionedRoom(name="logs", token="new", created=False, invited=False)]
        seeded, state = seed_channel_profile(db_path, "alice", rooms, force=True)

        assert seeded == {"log_channel": "new"}
        assert state == "updated"


class TestPendingChannelRooms:
    def test_keeps_everything_when_no_profile_exists(self, db_path):
        from istota.provision_rooms import DEFAULT_ROOMS, pending_channel_rooms

        assert pending_channel_rooms(db_path, "alice", DEFAULT_ROOMS) == DEFAULT_ROOMS

    def test_drops_a_channel_room_whose_column_is_already_set(self, db_path):
        # An operator pinned log_channel to a hand-made room. Creating a second
        # room called `logs` beside it and never using it is pure litter.
        from istota.provision_rooms import DEFAULT_ROOMS, pending_channel_rooms

        user_profiles.update_profile_with_status(db_path, "alice", log_channel="pinned")
        assert pending_channel_rooms(db_path, "alice", DEFAULT_ROOMS) == (
            "general", "alerts",
        )

    def test_general_is_never_dropped(self, db_path):
        from istota.provision_rooms import pending_channel_rooms

        user_profiles.update_profile_with_status(
            db_path, "alice", log_channel="p", alerts_channel="q"
        )
        assert pending_channel_rooms(db_path, "alice", ("general",)) == ("general",)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, **kwargs):
        defaults = {
            "config": None,
            "verbose": False,
            "user": None,
            "room": None,
            "no_seed": False,
            "reseed": False,
            "adopt": None,
            "json": False,
        }
        defaults.update(kwargs)
        self.__dict__.update(defaults)


@pytest.fixture
def cfg_file(tmp_path, db_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'db_path = "{db_path}"\n'
        f'temp_dir = "{tmp_path / "tmp"}"\n'
        "\n[nextcloud]\n"
        'url = "https://nc.example"\n'
        'username = "bot"\n'
        'app_password = "pw"\n'
    )
    return cfg


class TestProvisionRoomsCli:
    def test_prints_state_and_seeds_the_profile(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=True)
                for n in names
            ],
        )
        cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg_file), user="alice"))

        out = capsys.readouterr().out
        assert "STATE: created" in out
        profile = user_profiles.get_profile(db_path, "alice")
        assert profile.log_channel == "tok-logs"
        assert profile.alerts_channel == "tok-alerts"

    def test_repeat_run_reports_noop(self, cfg_file, db_path, monkeypatch, capsys):
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=False, invited=False)
                for n in names
            ],
        )
        args = _Args(config=str(cfg_file), user="alice")
        cli.cmd_nextcloud_provision_rooms(args)
        capsys.readouterr()
        cli.cmd_nextcloud_provision_rooms(args)

        assert "STATE: noop" in capsys.readouterr().out

    def test_skips_creating_a_room_for_a_pinned_channel(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        from istota import cli, provision_rooms as pr

        user_profiles.update_profile_with_status(db_path, "alice", log_channel="pinned")
        asked = {}

        def fake_provision(config, user_id, names, **kw):
            asked["names"] = names
            return [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=True) for n in names
            ]

        monkeypatch.setattr(pr, "provision_user_rooms", fake_provision)
        cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg_file), user="alice"))

        assert "logs" not in asked["names"]
        assert "general" in asked["names"]
        assert user_profiles.get_profile(db_path, "alice").log_channel == "pinned"

    def test_warns_and_reports_a_stranded_room(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=True, invited=False)
                for n in names
            ],
        )
        cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg_file), user="alice"))

        captured = capsys.readouterr()
        assert "invite FAILED" in captured.out
        assert "could not add" in captured.err
        # Nothing seeded: the user cannot read a room they are not in.
        assert user_profiles.get_profile(db_path, "alice") is None

    def test_no_seed_leaves_the_profile_alone(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=False)
                for n in names
            ],
        )
        cli.cmd_nextcloud_provision_rooms(
            _Args(config=str(cfg_file), user="alice", no_seed=True)
        )
        assert user_profiles.get_profile(db_path, "alice") is None

    def test_records_the_tokens_and_hands_them_back_on_the_next_run(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        # The whole ISSUE-342 loop through the entry point an operator runs:
        # run one records what it provisioned, run two is handed it back.
        from istota import cli, provision_rooms as pr

        seen: list = []

        def fake_provision(config, user_id, names, known_tokens=None, resolved=None):
            seen.append(known_tokens)
            return [
                pr.ProvisionedRoom(name=n, token=f"tok-{n}", created=False, invited=False)
                for n in names
            ]

        monkeypatch.setattr(pr, "provision_user_rooms", fake_provision)
        args = _Args(config=str(cfg_file), user="alice")
        cli.cmd_nextcloud_provision_rooms(args)
        cli.cmd_nextcloud_provision_rooms(args)

        assert seen[0] == {}
        assert seen[1]["general"] == "tok-general"
        assert pr.read_provisioned_tokens(db_path, "alice")["logs"] == "tok-logs"

    def test_a_failed_re_invite_is_stranded_and_not_reported_as_existing(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        # The room exists and the user cannot read it. Printing `existing` and
        # `STATE: noop` tells the Ansible `failed_when` nothing is wrong.
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(
                    name=n, token=f"tok-{n}", created=False, invited=False,
                    reinvited=True,
                )
                for n in names
            ],
        )
        cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg_file), user="alice"))

        captured = capsys.readouterr()
        assert "invite FAILED" in captured.out
        assert "STATE: updated" in captured.out
        assert "could not add" in captured.err

    def test_a_successful_re_invite_reports_updated(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        from istota import cli, provision_rooms as pr

        monkeypatch.setattr(
            pr,
            "provision_user_rooms",
            lambda config, user_id, names, **kw: [
                pr.ProvisionedRoom(
                    name=n, token=f"tok-{n}", created=False, invited=True,
                    reinvited=True,
                )
                for n in names
            ],
        )
        cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg_file), user="alice"))

        captured = capsys.readouterr()
        assert "re-invited" in captured.out
        assert "STATE: updated" in captured.out
        assert "invite FAILED" not in captured.out

    def test_a_partial_failure_still_records_what_resolved(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        # Those rooms exist on Talk. Losing their tokens puts the next deploy
        # back on name matching for them, which is the bug.
        import pytest as _pytest

        from istota import cli, provision_rooms as pr

        def fake_provision(config, user_id, names, known_tokens=None, resolved=None):
            resolved.append(
                pr.ProvisionedRoom(name=names[0], token="tok-first", created=True)
            )
            raise RuntimeError("Talk 503")

        monkeypatch.setattr(pr, "provision_user_rooms", fake_provision)
        with _pytest.raises(SystemExit):
            cli.cmd_nextcloud_provision_rooms(
                _Args(config=str(cfg_file), user="alice")
            )

        assert pr.read_provisioned_tokens(db_path, "alice") == {
            "general": "tok-first",
        }

    def test_adopt_records_a_token_without_contacting_talk(
        self, cfg_file, db_path, monkeypatch, capsys
    ):
        # The repair path for an install that already has the duplicate: the
        # record is empty and the kept room's name matches nothing.
        from istota import cli, provision_rooms as pr

        def explode(*a, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("--adopt must not provision")

        monkeypatch.setattr(pr, "provision_user_rooms", explode)
        cli.cmd_nextcloud_provision_rooms(
            _Args(config=str(cfg_file), user="alice", adopt=["general=tk4ab9cd"])
        )

        out = capsys.readouterr().out
        assert "recorded general = tk4ab9cd" in out
        assert "STATE: updated" in out
        assert pr.read_provisioned_tokens(db_path, "alice") == {"general": "tk4ab9cd"}

    def test_adopt_rejects_a_malformed_pair(self, cfg_file, db_path, capsys):
        import pytest as _pytest

        from istota import cli

        with _pytest.raises(SystemExit):
            cli.cmd_nextcloud_provision_rooms(
                _Args(config=str(cfg_file), user="alice", adopt=["general"])
            )
        assert "NAME=TOKEN" in capsys.readouterr().err

    def test_exits_when_nextcloud_is_not_configured(self, tmp_path, db_path, capsys):
        from istota import cli

        cfg = tmp_path / "bare.toml"
        cfg.write_text(f'db_path = "{db_path}"\ntemp_dir = "{tmp_path / "tmp"}"\n')
        with pytest.raises(SystemExit) as exc:
            cli.cmd_nextcloud_provision_rooms(_Args(config=str(cfg), user="alice"))
        assert exc.value.code != 0

    def test_subcommand_is_reachable_from_main(self, cfg_file, monkeypatch):
        # Guards the wiring: a handler nobody can reach fixes nothing.
        from istota import cli

        seen = {}
        monkeypatch.setattr(
            cli, "cmd_nextcloud_provision_rooms", lambda args: seen.update(vars(args))
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "istota", "-c", str(cfg_file),
                "nextcloud", "provision-rooms", "--user", "alice",
            ],
        )
        cli.main()

        assert seen["nextcloud_action"] == "provision-rooms"
        assert seen["user"] == "alice"


# ---------------------------------------------------------------------------
# Ansible wiring — the actual reported gap
# ---------------------------------------------------------------------------

TASK_NAME = "Provision default Talk rooms and seed log/alerts channels"


def _tasks():
    return yaml.safe_load(TASKS_FILE.read_text())


def _task(name):
    for task in _tasks():
        if isinstance(task, dict) and task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in tasks/main.yml")


class TestAnsibleProvisionsRooms:
    def test_task_exists_and_calls_the_cli(self):
        task = _task(TASK_NAME)
        env = Environment()
        rendered = env.from_string(task["command"]).render(
            istota_home="/srv/app/istota",
            istota_package="istota",
            istota_repo_dir="/srv/app/istota",
            user_item={"key": "alice", "value": {}},
        )
        assert "nextcloud provision-rooms" in rendered
        assert "--user alice" in rendered

    def test_runs_after_the_user_profile_row_exists(self):
        # Seeding writes into user_profiles; running before `user ensure`
        # would insert a bare row and then have inventory values applied on
        # top, inverting the precedence the seeding rule depends on.
        names = [t.get("name") for t in _tasks() if isinstance(t, dict)]
        assert names.index("Ensure user_profiles rows") < names.index(TASK_NAME)

    def test_changed_when_follows_the_state_line(self):
        task = _task(TASK_NAME)
        assert "STATE: noop" in task["changed_when"]

    def test_gated_on_nextcloud_being_configured(self):
        task = _task(TASK_NAME)
        conditions = task["when"]
        if isinstance(conditions, str):
            conditions = [conditions]
        joined = " ".join(conditions)
        assert "istota_web_only" in joined
        assert "istota_nextcloud_url" in joined
        assert "istota_provision_talk_rooms" in joined
        # Talk configured off must not still get three Talk rooms.
        assert "istota_talk_enabled" in joined

    def test_passes_the_app_password_in_the_environment(self):
        # `istota_use_environment_file` defaults to true, so config.toml.j2
        # deliberately omits app_password — it reaches the daemon through
        # systemd's EnvironmentFile, which an Ansible `command:` never reads.
        # Without this the CLI finds no credential and exits 1 on every deploy.
        task = _task(TASK_NAME)
        env = task.get("environment") or {}
        assert "ISTOTA_NEXTCLOUD_APP_PASSWORD" in env
        assert "istota_nextcloud_app_password" in env["ISTOTA_NEXTCLOUD_APP_PASSWORD"]

    def test_skipped_when_no_app_password_is_configured(self):
        task = _task(TASK_NAME)
        conditions = task["when"]
        if isinstance(conditions, str):
            conditions = [conditions]
        assert any("istota_nextcloud_app_password" in c for c in conditions)

    def test_retries_a_transient_nextcloud(self):
        # spreed migrations lag a Nextcloud upgrade; the docker entrypoint waits
        # up to two minutes for the same reason.
        task = _task(TASK_NAME)
        assert int(task["retries"]) >= 2
        assert "until" in task

    def test_a_failed_invite_fails_the_task(self):
        # A room the user was never added to is one they cannot read.
        task = _task(TASK_NAME)
        assert "invite FAILED" in task["failed_when"]

    def test_restarts_the_services_that_snapshot_the_profile(self):
        task = _task(TASK_NAME)
        assert "restart istota-scheduler" in task["notify"]

    def test_defaults_expose_the_toggle(self):
        defaults = yaml.safe_load(DEFAULTS_FILE.read_text())
        assert defaults["istota_provision_talk_rooms"] is True


class TestDockerEntrypointMatchesTheCli:
    def test_entrypoint_creates_group_rooms_not_public_ones(self):
        # Both deploy targets must agree on room privacy, or the same install
        # documented two ways behaves differently.
        body = ENTRYPOINT.read_text()
        start = body.index("create_group_room() {")
        end = body.index("post_room_message() {")
        assert '\\"roomType\\":2' in body[start:end]


# ---------------------------------------------------------------------------
# ISSUE-342 — a renamed room must not be re-created on the next deploy
# ---------------------------------------------------------------------------


class TestProvisionedTokenRecord:
    """The token this tool provisioned is remembered, so a rename is survivable.

    `ensure_room` recognised "its" room by display name alone. Rename the
    conversation on either surface and the next run no longer matches it, so it
    mints a fresh one under the old name — and the Ansible role runs this on
    every deploy. `general` was the worst case: `pending_channel_rooms` protects
    `logs` and `alerts` once their profile columns hold a token, and `general`
    has no column, so it was re-derived from its name for ever.
    """

    def test_the_namespace_is_reserved_from_the_model(self):
        from istota.kv_namespaces import is_reserved_namespace
        from istota.provision_rooms import PROVISIONED_NAMESPACE

        assert is_reserved_namespace(PROVISIONED_NAMESPACE)

    def test_record_and_read_round_trip(self, tmp_path):
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_tokens,
            record_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        assert read_provisioned_tokens(db_path, "alice") == {}

        assert record_provisioned_tokens(db_path, "alice", [
            ProvisionedRoom(name="general", token="G1", created=True),
            ProvisionedRoom(name="logs", token="L1", created=True),
        ]) is True
        assert read_provisioned_tokens(db_path, "alice") == {
            "general": "G1", "logs": "L1",
        }

    def test_a_changed_token_overwrites_the_record(self, tmp_path):
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_tokens,
            record_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        record_provisioned_tokens(
            db_path, "alice",
            [ProvisionedRoom(name="general", token="G1", created=True)],
        )
        record_provisioned_tokens(
            db_path, "alice",
            [ProvisionedRoom(name="general", token="G2", created=True)],
        )
        assert read_provisioned_tokens(db_path, "alice")["general"] == "G2"

    def test_the_value_is_json_like_every_other_kv_writer(self, tmp_path):
        # `cli.cmd_kv_get` does an unguarded `json.loads` on a stored value, so
        # a bare string there is a traceback rather than a value.
        import json

        from istota.provision_rooms import (
            PROVISIONED_NAMESPACE,
            ProvisionedRoom,
            record_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        record_provisioned_tokens(
            db_path, "alice",
            [ProvisionedRoom(name="general", token="G1", created=True)],
        )
        with db.get_db(db_path) as conn:
            raw = db.kv_get(conn, "alice", PROVISIONED_NAMESPACE, "general")
        assert json.loads(raw["value"]) == "G1"

    def test_a_bare_string_from_an_earlier_version_is_still_read(self, tmp_path):
        from istota.provision_rooms import (
            PROVISIONED_NAMESPACE,
            read_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.kv_set(conn, "alice", PROVISIONED_NAMESPACE, "general", "G1")
        assert read_provisioned_tokens(db_path, "alice") == {"general": "G1"}

    def test_the_record_is_per_user(self, tmp_path):
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_tokens,
            record_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        record_provisioned_tokens(
            db_path, "alice",
            [ProvisionedRoom(name="general", token="G1", created=True)],
        )
        assert read_provisioned_tokens(db_path, "bob") == {}

    def test_a_room_with_no_token_is_not_recorded(self, tmp_path):
        from istota.provision_rooms import (
            ProvisionedRoom,
            read_provisioned_tokens,
            record_provisioned_tokens,
        )

        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        record_provisioned_tokens(
            db_path, "alice",
            [ProvisionedRoom(name="general", token="", created=False)],
        )
        assert read_provisioned_tokens(db_path, "alice") == {}

    def test_recording_against_a_missing_db_does_not_raise(self, tmp_path):
        # Bookkeeping runs after the rooms already exist on Talk, so a DB
        # failure here must not turn a successful provision into a failed play.
        from istota.provision_rooms import ProvisionedRoom, record_provisioned_tokens

        assert record_provisioned_tokens(
            tmp_path / "nope.db", "alice",
            [ProvisionedRoom(name="general", token="G1", created=True)],
        ) is False

    def test_reading_a_missing_db_returns_empty(self, tmp_path):
        from istota.provision_rooms import read_provisioned_tokens

        assert read_provisioned_tokens(tmp_path / "nope.db", "alice") == {}


class TestRenameDoesNotDuplicate:
    @pytest.mark.asyncio
    async def test_a_renamed_room_is_reused_by_token(self):
        # The ISSUE-342 regression: `general` was renamed to `#general` in the
        # web UI, which propagated to Talk. The next deploy found no group room
        # called `general` and made one.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.token, result.created) == ("G1", False)
        client.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_renamed_room_the_user_left_is_re_invited_not_recreated(self):
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "bot"}]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.token, result.created) == ("G1", False)
        assert result.invited is True
        client.create_conversation.assert_not_awaited()
        client.add_participant.assert_awaited_once_with("G1", "alice")

    @pytest.mark.asyncio
    async def test_a_reused_room_does_not_reseed_a_cleared_channel(self):
        # An empty `log_channel` is a user-chosen state (ISSUE-115). Reusing a
        # recorded room must not count as making it usable, or a deploy would
        # switch the execution log back on.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "L1", "displayName": "#logs", "type": 2}],
            participants={"L1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(
            client, "logs", "alice", bot_user_id="bot", known_token="L1",
        )

        assert result.seedable is False

    @pytest.mark.asyncio
    async def test_a_recorded_token_that_no_longer_exists_falls_back_to_the_name(self):
        # The conversation was deleted in Nextcloud, or the bot was removed from
        # it. The name path has to take over — and the room list carries a
        # same-named room under a *different* token, so reusing it is
        # distinguishable from simply creating one. Against an empty room list
        # this test would pass whether the fallback ran or not.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G2", "displayName": "general", "type": 2}],
            participants={"G2": [{"actorType": "users", "actorId": "alice"}]},
            created_token="G3",
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="GONE",
        )

        assert (result.token, result.created) == ("G2", False)
        client.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_recorded_token_gone_with_no_name_match_creates(self):
        from istota.provision_rooms import ensure_room

        client = _fake_client(rooms=[], created_token="G2")
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="GONE",
        )

        assert (result.token, result.created) == ("G2", True)

    @pytest.mark.asyncio
    async def test_a_link_shared_room_is_still_recognised(self):
        # Link-sharing the room makes it type 3. The token carries the identity,
        # so requiring type 2 here would reject it, the name match would fail
        # too (it was renamed — the whole premise), and the duplicate is back.
        # Only the DM-ish types are refused on this path.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 3}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
            created_token="G2",
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.token, result.created) == ("G1", False)

    @pytest.mark.asyncio
    async def test_the_user_is_not_dragged_back_into_a_room_they_left(self):
        # Other humans are in it, so the user left a shared room. Re-adding them
        # on every deploy is the ISSUE-102 clobber shape, and unlike logs/alerts
        # there is no column they could clear to opt out.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [
                {"actorType": "users", "actorId": "bot"},
                {"actorType": "users", "actorId": "carol"},
            ]},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.token, result.created) == ("G1", False)
        assert (result.reinvited, result.invited) == (False, False)
        client.add_participant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_participant_list_is_read_as_a_failed_read(self):
        # `_is_orphan` already treats an empty list as more likely a failed read
        # than a real room; the two paths must not disagree about the same
        # evidence.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": []},
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert result.token == "G1"
        assert result.reinvited is False
        client.add_participant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_re_invite_is_marked_so_the_cli_can_report_it(self):
        # `invited=False` on this path meant both "already in it" and "tried and
        # failed", and the CLI's stranded predicate reads only created/adopted —
        # so a room the user cannot read printed `existing` and the play passed.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "#general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "bot"}]},
        )
        client.add_participant = AsyncMock(side_effect=RuntimeError("no permission"))
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.invited, result.reinvited) == (False, True)
        assert result.seedable is False

    @pytest.mark.asyncio
    async def test_a_recorded_token_pointing_at_a_one_to_one_is_ignored(self):
        # Talk puts the other party's user id in `name` for a DM. A recorded
        # token must not resurrect one as a channel.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "alice", "type": 1}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
            created_token="G2",
        )
        result = await ensure_room(
            client, "general", "alice", bot_user_id="bot", known_token="G1",
        )

        assert (result.token, result.created) == ("G2", True)

    @pytest.mark.asyncio
    async def test_no_record_still_matches_by_name(self):
        # The first-provision path is unchanged.
        from istota.provision_rooms import ensure_room

        client = _fake_client(
            rooms=[{"token": "G1", "displayName": "general", "type": 2}],
            participants={"G1": [{"actorType": "users", "actorId": "alice"}]},
        )
        result = await ensure_room(client, "general", "alice", known_token=None)

        assert (result.token, result.created) == ("G1", False)

    @pytest.mark.asyncio
    async def test_provision_rooms_threads_the_record_through(self):
        from istota.provision_rooms import provision_rooms

        client = _fake_client(
            rooms=[
                {"token": "G1", "displayName": "#general", "type": 2},
                {"token": "L1", "displayName": "logs", "type": 2},
            ],
            participants={
                "G1": [{"actorType": "users", "actorId": "alice"}],
                "L1": [{"actorType": "users", "actorId": "alice"}],
            },
        )
        results = await provision_rooms(
            client, "alice", ("general", "logs"),
            bot_user_id="bot", known_tokens={"general": "G1"},
        )

        assert [(r.name, r.token, r.created) for r in results] == [
            ("general", "G1", False), ("logs", "L1", False),
        ]
        client.create_conversation.assert_not_awaited()
