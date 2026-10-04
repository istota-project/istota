"""A surface's name for a room flows into `rooms.name` only when it moved there.

ISSUE-637: a web rename of a WhatsApp group's room reverted on the next roster
frame, because `apply_roster` renamed whenever the group subject differed from
`rooms.name`. The Talk ingest and the poller's title backfill made the same
comparison, masked only by the web rename also renaming the Talk conversation.
Each writer now compares against the last name it saw on its own surface
(`room_bindings.external_name`), so a web rename survives an unchanged external
name while a rename on the external side still comes through.
"""

from __future__ import annotations

import sqlite3

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.transport.ingest import record_inbound
from istota.transport.talk import inbound as poller
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp.webhook import handle_whatsapp_batch

from .support.whatsapp_config import build_whatsapp_config

BAILEYS = db.WHATSAPP_BAILEYS_PROVIDER
GROUP = "120363000000000001@g.us"
ALICE_JID = "15551234567@s.whatsapp.net"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _name(db_path, surface, ref):
    with db.get_db(db_path) as conn:
        return db.get_room(conn, db.resolve_room_token(conn, surface, ref)).name


def _web_rename(db_path, surface, ref, name):
    with db.get_db(db_path) as conn:
        db.rename_room(conn, db.resolve_room_token(conn, surface, ref), name)


class TestWhatsAppGroupSubject:
    @pytest.fixture
    def config(self, tmp_path, db_path):
        config = Config(
            db_path=db_path,
            temp_dir=tmp_path / "tmp",
            whatsapp=build_whatsapp_config(
                enabled=True, provider=BAILEYS, business_phone_number="+15551230000",
            ),
            users={"alice": UserConfig(display_name="Alice")},
        )
        with db.get_db(db_path) as conn:
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
            db.latch_whatsapp_jid(conn, "alice", jid=ALICE_JID)
        return config

    def _roster(self, config, subject):
        event = proto.group_roster({
            "group_jid": GROUP, "subject": subject, "added_by": ALICE_JID,
            "bot_present": True,
            "participants": [{"jid": ALICE_JID, "lid": ""}],
        })
        with db.get_db(config.db_path) as conn:
            return handle_whatsapp_batch(conn, config, [event], provider=BAILEYS)

    def test_a_web_rename_survives_an_unchanged_subject(self, config, db_path):
        self._roster(config, "Family")
        _web_rename(db_path, "whatsapp", GROUP, "Kids")

        self._roster(config, "Family")

        assert _name(db_path, "whatsapp", GROUP) == "Kids"

    def test_a_subject_changed_in_whatsapp_still_renames(self, config, db_path):
        self._roster(config, "Family")
        _web_rename(db_path, "whatsapp", GROUP, "Kids")

        self._roster(config, "Cousins")

        assert _name(db_path, "whatsapp", GROUP) == "Cousins"


class TestTalkIngest:
    def _inbound(self, db_path, channel_name):
        config = Config()
        config.db_path = db_path
        with db.get_db(db_path) as conn:
            record_inbound(conn, config, surface="talk", surface_ref="cpz",
                           user_id="alice", text="hi", channel_name=channel_name)

    def test_a_web_rename_survives_an_unchanged_channel_name(self, db_path):
        """The web rename's push to Talk is best-effort; when it fails, the
        next inbound still carries the old Talk name."""
        self._inbound(db_path, "Old")
        _web_rename(db_path, "talk", "cpz", "Web name")

        self._inbound(db_path, "Old")

        assert _name(db_path, "talk", "cpz") == "Web name"

    def test_a_rename_in_talk_still_renames(self, db_path):
        self._inbound(db_path, "Old")
        _web_rename(db_path, "talk", "cpz", "Web name")

        self._inbound(db_path, "Renamed in Talk")

        assert _name(db_path, "talk", "cpz") == "Renamed in Talk"


class TestTalkPollerBackfill:
    def _config(self, tmp_path, db_path):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass",
        )
        config.scheduler = SchedulerConfig()
        return config

    def _pass(self, tmp_path, db_path, display_name):
        plan = poller._RoomPlan(
            conv={"token": "grp", "type": 2}, token="grp", conv_type=2,
            display_name=display_name, canonical="grp", known_cursor=10,
            last_message_id=10, needs_participants=False,
            needs_cursor_init=False, needs_backfill=False,
            participants=[], latest_id=10,
        )
        with db.get_db(db_path) as conn:
            poller._apply_room_pass(
                conn, self._config(tmp_path, db_path), object(), [plan],
                full_sweep=True, open_poll=lambda *a, **k: None,
            )

    def _register(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "grp", "alice", origin="talk", name="team")
            db.add_room_binding(conn, "grp", "talk", "grp")

    def test_a_web_rename_survives_an_unchanged_display_name(self, tmp_path, db_path):
        self._register(db_path)
        self._pass(tmp_path, db_path, "team")
        _web_rename(db_path, "talk", "grp", "Web name")

        self._pass(tmp_path, db_path, "team")

        assert _name(db_path, "talk", "grp") == "Web name"

    def test_a_rename_in_talk_still_renames(self, tmp_path, db_path):
        self._register(db_path)
        self._pass(tmp_path, db_path, "team")
        _web_rename(db_path, "talk", "grp", "Web name")

        self._pass(tmp_path, db_path, "renamed")

        assert _name(db_path, "talk", "grp") == "renamed"


class TestTheRule:
    """`db.observe_external_room_name` directly, for the cases no writer
    reaches on its own path."""

    @pytest.fixture
    def conn(self, db_path):
        with db.get_db(db_path) as connection:
            yield connection

    def test_with_no_last_seen_name_a_named_room_keeps_its_name(self, conn):
        """A WhatsApp binding from before the column: the room's name may be a
        web rename no roster has reverted yet, so a differing subject is not
        evidence of a rename in WhatsApp. Recorded, and taken next time it
        moves."""
        db.register_room(conn, "r", "alice", origin="whatsapp", name="Kids")
        db.add_room_binding(conn, "r", "whatsapp", GROUP)

        assert db.observe_external_room_name(conn, "r", "whatsapp", "Family") is False
        assert db.get_room(conn, "r").name == "Kids"
        assert db.observe_external_room_name(conn, "r", "whatsapp", "Cousins") is True
        assert db.get_room(conn, "r").name == "Cousins"

    def test_a_room_with_no_binding_only_takes_a_name_it_lacks(self, conn):
        db.register_room(conn, "named", "alice", origin="talk", name="Mine")
        db.register_room(conn, "unnamed", "alice", origin="talk", name=None)

        assert db.observe_external_room_name(conn, "named", "talk", "Theirs") is False
        assert db.get_room(conn, "named").name == "Mine"
        assert db.observe_external_room_name(conn, "unnamed", "talk", "Theirs") is True
        assert db.get_room(conn, "unnamed").name == "Theirs"

    def test_an_empty_external_name_changes_nothing(self, conn):
        db.register_room(conn, "r", "alice", origin="talk", name="Mine")
        db.add_room_binding(conn, "r", "talk", "r")

        assert db.observe_external_room_name(conn, "r", "talk", "") is False
        assert db.get_room(conn, "r").name == "Mine"


def test_an_upgraded_database_gains_the_column_with_talk_names_seeded(tmp_path):
    """`room_bindings` from before the column gains it from the migration,
    which `schema.sql`'s `CREATE IF NOT EXISTS` would not do. A Talk binding
    starts from the room's name, since the old writers kept the two equal; a
    WhatsApp one stays unknown, since a web rename may be standing there."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE rooms (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
            "name TEXT, origin TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "archived INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE room_bindings (room_token TEXT NOT NULL, surface TEXT NOT NULL, "
            "surface_ref TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "PRIMARY KEY (room_token, surface))"
        )
        conn.executemany(
            "INSERT INTO rooms (token, user_id, name, origin) VALUES (?, 'alice', ?, ?)",
            [("t", "Team", "talk"), ("w", "Kids", "whatsapp")],
        )
        conn.executemany(
            "INSERT INTO room_bindings (room_token, surface, surface_ref) VALUES (?, ?, ?)",
            [("t", "talk", "tref"), ("w", "whatsapp", GROUP)],
        )
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        names = dict(conn.execute("SELECT surface, external_name FROM room_bindings"))
    assert names == {"talk": "Team", "whatsapp": None}


class TestTheWebRenamePushedToTalk:
    """A web rename that reached Talk is Talk's name now, so the next poll
    reading it back is not a rename in Talk."""

    @pytest.fixture
    async def client(self, tmp_path, db_path):
        pytest.importorskip("fastapi")
        pytest.importorskip("authlib")
        from unittest.mock import AsyncMock, MagicMock

        from httpx import ASGITransport, AsyncClient

        import istota.webui.app as mod
        from istota.config import SiteConfig, WebConfig

        config = Config(
            db_path=db_path,
            workspace_path=tmp_path / "mount",
            site=SiteConfig(hostname="example.com"),
            nextcloud=NextcloudConfig(url="https://nc.test", username="istota",
                                      app_password="pass"),
            users={"alice": UserConfig(display_name="Alice")},
            web=WebConfig(
                enabled=True, port=8766,
                oauth2_provider="https://cloud.example.com",
                oauth2_client_id="istota-web", oauth2_client_secret="s",
                session_secret_key="test-session-key",
            ),
        )
        mod._config = config
        mod.app.state.istota_config = config
        mod._oauth = MagicMock()
        mod._oauth.nextcloud = MagicMock()
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            return_value={"user_id": "alice"},
        )
        transport = ASGITransport(app=mod.app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as c:
            c.cookies = (await c.get("/istota/callback", follow_redirects=False)).cookies
            yield c

    async def _rename(self, client, db_path, name, *, push_fails):
        from unittest.mock import AsyncMock, MagicMock, patch

        with db.get_db(db_path) as conn:
            token = db.resolve_room_token(conn, "talk", "cpz")
            handle = db.ensure_web_chat_handle(conn, "alice", token, "Old")
        talk = MagicMock()
        talk.rename_conversation = AsyncMock(
            side_effect=RuntimeError("NC down") if push_fails else None,
        )
        talk.aclose = AsyncMock()
        with patch("istota.nextcloud.talk.TalkClient", return_value=talk):
            resp = await client.patch(
                f"/istota/api/chat/rooms/{handle.id}", json={"name": name},
                headers={"origin": "https://example.com"},
            )
        assert resp.status_code == 200
        talk.rename_conversation.assert_awaited_once()

    def _talk_room(self, db_path):
        config = Config()
        config.db_path = db_path
        with db.get_db(db_path) as conn:
            record_inbound(conn, config, surface="talk", surface_ref="cpz",
                           user_id="alice", text="hi", channel_name="Old")

    def _talk_says(self, db_path, channel_name):
        config = Config()
        config.db_path = db_path
        with db.get_db(db_path) as conn:
            record_inbound(conn, config, surface="talk", surface_ref="cpz",
                           user_id="alice", text="again", channel_name=channel_name)

    async def test_a_second_web_rename_survives_the_first_ones_echo(self, client, db_path):
        self._talk_room(db_path)
        await self._rename(client, db_path, "W", push_fails=False)
        await self._rename(client, db_path, "W2", push_fails=True)

        self._talk_says(db_path, "W")

        assert _name(db_path, "talk", "cpz") == "W2"

    async def test_a_failed_push_records_nothing(self, client, db_path):
        self._talk_room(db_path)
        await self._rename(client, db_path, "W", push_fails=True)

        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT external_name FROM room_bindings WHERE surface = 'talk'"
            ).fetchone()
        assert row["external_name"] == "Old"
