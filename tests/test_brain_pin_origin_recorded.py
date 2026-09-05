"""The namespace a model pin was written in is recorded, not inferred (ISSUE-420).

`executor._pin_origin_namespace` used to answer from `tasks.brain`: a pinned
task's origin was the pinned kind's namespace, on the premise that every writer
of `rooms.model` resolves through that same kind. ISSUE-417 relied on that and
it is true *at the moment of writing*. It stops being true the moment the
operator shortens `[brain] room_selectable`, because the two facts then move
apart:

- `commands.brain_for_room` hands the pin to `resolve_brain_kind`, which
  **refuses** a kind absent from the allowlist and falls through to the lane. So
  a `!room model` typed after the kind was dropped resolves in the *lane's*
  namespace.
- `rooms.brain` still names the dropped kind, and `record_inbound` copied it
  onto `tasks.brain` raw.

Origin then read one namespace while the id was written in another, the crossing
rule fired on a crossing that had not happened, and the model was dropped — on
every turn in that room, at INFO, with the task running the brain's own default
instead of the model the user chose.

Neither producer can be fixed on its own, and that is the point. Writing only an
admitted pin at ingest (the ISSUE-419 cron rule) discards the case ISSUE-417
exists for: a model written *while* the pin was admitted really is in the
dropped kind's namespace, and inferring from the lane would drop it wrongly. The
two cases leave identical rows behind, so nothing on the row distinguishes them.
Recording the namespace *with* the pin is what separates them, and it answers
ISSUE-421(c) — one `rooms.model` shared by every bound surface, written against
the writing surface's lane and read against the inbound one — by the same
mechanism.

A row that predates the column carries NULL, which falls through to the old
inference. That is deliberate: it is exactly today's behaviour, so the upgrade
changes no existing room's answer, and the first write to a room's model records
the fact from then on.
"""

import pytest

from istota import db
from istota.brain import model_namespace_for_kind
from istota.commands import CommandContext, cmd_room
from istota.config import (
    BrainConfig,
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.executor import _pin_origin_namespace, _request_model
from istota.transport.ingest import record_inbound


class _Task:
    """Only the fields the origin rule and the request build read."""

    def __init__(self, model="", effort="", brain=None, source_type="",
                 model_namespace=None):
        self.model = model
        self.effort = effort
        self.brain = brain
        self.source_type = source_type
        self.model_namespace = model_namespace


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path, *, kind="claude_code", room_selectable=(),
            source_type_overrides=None):
    cfg = Config()
    cfg.db_path = db_path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.talk = TalkConfig(enabled=True, bot_username="istota")
    cfg.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="p",
    )
    cfg.scheduler = SchedulerConfig()
    cfg.users = {"alice": UserConfig()}
    cfg.brain = BrainConfig(
        kind=kind,
        room_selectable=list(room_selectable),
        source_type_overrides=dict(source_type_overrides or {}),
    )
    return cfg


def _ctx(config, conn, args, user_id="alice", token="room1", surface="talk"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface=surface,
    )


class TestTheColumnCarriesTheOrigin:
    """`_pin_origin_namespace` prefers the stored fact over every inference."""

    def test_a_stored_namespace_outranks_the_brain_column(self, db_path, tmp_path):
        """The ISSUE-420 shape, at the rule itself.

        `brain='native'` would infer `openai_compat`; the id was actually
        resolved in `anthropic` because the allowlist refused the pin.
        """
        config = _config(db_path, tmp_path, room_selectable=[])
        task = _Task(
            "claude-opus-5", brain="native", source_type="talk",
            model_namespace="anthropic",
        )
        assert _pin_origin_namespace(task, config) == "anthropic"

    def test_a_stored_namespace_outranks_the_lane(self, db_path, tmp_path):
        """ISSUE-421(c): written from web (`anthropic`), read on Talk, which
        this deployment routes to native. The stored fact wins over both."""
        config = _config(
            db_path, tmp_path, source_type_overrides={"talk": "native"},
        )
        task = _Task(
            "claude-opus-5", source_type="talk", model_namespace="anthropic",
        )
        assert _pin_origin_namespace(task, config) == "anthropic"

    def test_the_model_survives_the_request_build(self, db_path, tmp_path):
        """The consequence the user actually sees: the pin reaches the request
        instead of being dropped for a crossing that did not happen."""
        config = _config(db_path, tmp_path, room_selectable=[])
        from istota.brain import make_brain
        task = _Task(
            "claude-opus-5", brain="native", source_type="talk",
            model_namespace="anthropic",
        )
        assert _request_model(
            task, config, make_brain(BrainConfig(kind="claude_code")),
        ) == "claude-opus-5"

    def test_a_genuine_crossing_still_drops(self, db_path, tmp_path):
        """The stored fact is an origin, not an exemption. A pin recorded in
        `openai_compat` meeting an `anthropic` brain still crosses."""
        config = _config(db_path, tmp_path)
        from istota.brain import make_brain
        task = _Task(
            "z-ai/glm-5.3-flash", source_type="talk",
            model_namespace="openai_compat",
        )
        assert _request_model(
            task, config, make_brain(BrainConfig(kind="claude_code")),
        ) == ""

    def test_no_stored_namespace_falls_through_to_the_old_inference(
        self, db_path, tmp_path,
    ):
        """A row written before the column exists. NULL is not "no crossing" —
        it is "not recorded", and the ISSUE-417 inference still answers it."""
        config = _config(db_path, tmp_path, room_selectable=[])
        task = _Task("claude-opus-5", brain="native", source_type="talk")
        assert _pin_origin_namespace(task, config) == "openai_compat"


class TestTheRoomRecordsItAsItWrites:
    """`!room model` stores the namespace of the brain it resolved against."""

    async def test_a_room_write_records_the_namespace(self, db_path, tmp_path):
        config = _config(db_path, tmp_path, room_selectable=["native"])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            await cmd_room(_ctx(config, conn, "model smart"))
            room = db.get_room(conn, "room1")
        assert room.model is not None
        assert room.model_namespace == model_namespace_for_kind("claude_code")

    async def test_a_refused_pin_records_the_lane_it_actually_resolved_in(
        self, db_path, tmp_path,
    ):
        """The ISSUE-420 repro, at the producer.

        The room pins `native`; the operator has since emptied the allowlist, so
        `brain_for_room` refuses it and resolves the alias against
        `claude_code`. The namespace recorded must be the one it resolved in,
        not the one the column names.
        """
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            await cmd_room(_ctx(config, conn, "model smart"))
            room = db.get_room(conn, "room1")
        assert room.brain == "native"
        assert room.model_namespace == "anthropic"

    async def test_clearing_the_model_clears_the_namespace(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            await cmd_room(_ctx(config, conn, "model smart"))
            await cmd_room(_ctx(config, conn, "model default"))
            room = db.get_room(conn, "room1")
        assert room.model is None
        assert room.model_namespace is None

    async def test_setting_effort_alone_leaves_the_namespace_alone(
        self, db_path, tmp_path,
    ):
        """`!room effort` moves one knob. The namespace belongs to the model."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            await cmd_room(_ctx(config, conn, "model smart"))
            before = db.get_room(conn, "room1").model_namespace
            await cmd_room(_ctx(config, conn, "effort high"))
            room = db.get_room(conn, "room1")
        assert before == "anthropic"
        assert room.model_namespace == before


class TestTheTaskCarriesItFromTheRoom:
    """`record_inbound` freezes the stored namespace onto the task, beside the
    model it belongs to."""

    def test_the_room_default_carries_its_namespace(self, db_path, tmp_path):
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            db.set_room_model_effort(
                conn, "room1", "claude-opus-5", None, namespace="anthropic",
            )
            _token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="hello", source_type="talk",
            )
            task = db.get_task(conn, task_id)
        assert task.brain == "native"
        assert task.model == "claude-opus-5"
        assert task.model_namespace == "anthropic"

    async def test_the_whole_chain_keeps_the_users_model(self, db_path, tmp_path):
        """End to end: the ISSUE-420 room, one turn, model intact.

        Before the fix this task ran the brain's own default and said so only at
        INFO.
        """
        config = _config(db_path, tmp_path, room_selectable=[])
        from istota.brain import make_brain
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            await cmd_room(_ctx(config, conn, "model smart"))
            chosen = db.get_room(conn, "room1").model
            _token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="hello", source_type="talk",
            )
            task = db.get_task(conn, task_id)
        assert _request_model(
            task, config, make_brain(BrainConfig(kind="claude_code")),
        ) == chosen

    def test_a_room_with_no_recorded_namespace_is_unchanged(
        self, db_path, tmp_path,
    ):
        """An upgraded deployment's existing room: NULL travels, and the task
        falls through to the inference exactly as it did before."""
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            conn.execute(
                "UPDATE rooms SET model = ? WHERE token = ?",
                ("claude-opus-5", "room1"),
            )
            _token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="hello", source_type="talk",
            )
            task = db.get_task(conn, task_id)
        assert task.model_namespace is None
        assert _pin_origin_namespace(task, config) == "openai_compat"

    def test_an_inline_pin_records_the_namespace_it_resolved_in(
        self, db_path, tmp_path,
    ):
        """The same defect one step earlier: an inline `!model` prefix is
        resolved against the room's *admitted* brain
        (`talk/inbound.py` builds `make_brain(brain_for_room(...))`), so a
        refused room pin puts the id in the lane's namespace while
        `tasks.brain` still names the refused kind."""
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            _token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="hello", source_type="talk",
                model="claude-opus-5", apply_room_default=False,
            )
            task = db.get_task(conn, task_id)
        assert task.brain == "native"
        assert task.model_namespace == "anthropic"

    def test_an_unpinned_turn_records_nothing(self, db_path, tmp_path):
        """No model, no namespace — the column describes a pin, and a row with
        one and not the other would be a lie the crossing rule reads."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            _token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="hello", source_type="talk",
            )
            task = db.get_task(conn, task_id)
        assert task.model is None
        assert task.model_namespace is None


class TestTheColumnSurvivesTheStore:
    def test_rooms_roundtrip(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            db.set_room_model_effort(
                conn, "room1", "m", "high", namespace="openai_compat",
            )
            assert db.get_room(conn, "room1").model_namespace == "openai_compat"
            db.set_room_model(conn, "room1", "m2", namespace="anthropic")
            assert db.get_room(conn, "room1").model_namespace == "anthropic"

    def test_a_new_room_has_none(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web")
            assert db.get_room(conn, "room1").model_namespace is None

    def test_tasks_roundtrip(self, db_path):
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="p", user_id="alice", source_type="talk",
                model="m", model_namespace="anthropic",
            )
            assert db.get_task(conn, task_id).model_namespace == "anthropic"

    def test_the_migration_is_idempotent(self, db_path):
        db.init_db(db_path)
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)")}
            task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "model_namespace" in cols
        assert "model_namespace" in task_cols


class TestTheClearingRuleReadsTheSameFact:
    """`!brain` must prefer the recorded namespace too, or the two readers of
    one row disagree.

    `_clear_pin_across_namespaces` drops a room's model pin when the brain
    change crosses a namespace, and it inferred the outgoing namespace from
    `rooms.brain` — the same inference `_pin_origin_namespace` used, wrong in
    the same case and for the same reason. Left alone it undoes this fix from
    the other end: the executor keeps the pin turn after turn, and then one
    `!brain` throws it away and tells the user it belonged to the outgoing
    brain's namespace, which the recorded fact contradicts.
    """

    async def _room(self, conn, config, *, pin, args="model smart"):
        db.register_room(conn, "room1", "alice", origin="talk")
        db.set_room_brain(conn, "room1", pin)
        await cmd_room(_ctx(config, conn, args))
        return db.get_room(conn, "room1")

    async def test_a_refused_pin_moving_within_its_real_namespace_is_kept(
        self, db_path, tmp_path,
    ):
        """The ISSUE-420 room, then `!brain claude_code`.

        `brain_for_room` refused `native`, so the id was resolved in
        `anthropic` and is moving to `anthropic`. Nothing crosses and the pin
        must survive. The inference reads `openai_compat` off the column and
        would clear it.
        """
        from istota.commands import _clear_pin_across_namespaces
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            room = await self._room(conn, config, pin="native")
            assert room.model_namespace == "anthropic"
            cleared = _clear_pin_across_namespaces(
                config, conn, "room1", room,
                source_type="talk", outgoing="native", incoming="claude_code",
            )
            after = db.get_room(conn, "room1")
        assert cleared == []
        assert after.model == room.model
        assert after.model_namespace == "anthropic"

    def test_a_genuine_crossing_still_clears(self, db_path, tmp_path):
        """The rule is still a rule. An admitted `native` pin really does put
        the id in `openai_compat`, and moving to `claude_code` crosses.

        The row is written directly rather than through `!room model`: the
        subject here is the clearing rule, and native's alias table does not
        carry `smart`, so driving the producer would set no model at all and the
        rule would return early on `not room.model` — passing for a reason that
        has nothing to do with namespaces.
        """
        from istota.commands import _clear_pin_across_namespaces
        config = _config(db_path, tmp_path, room_selectable=["native"])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            db.set_room_model_effort(
                conn, "room1", "z-ai/glm-5.3-flash", "high",
                namespace="openai_compat",
            )
            room = db.get_room(conn, "room1")
            assert room.model_namespace == "openai_compat"
            cleared = _clear_pin_across_namespaces(
                config, conn, "room1", room,
                source_type="talk", outgoing="native", incoming="claude_code",
            )
            after = db.get_room(conn, "room1")
        assert cleared != []
        assert after.model is None
        assert after.model_namespace is None

    def test_a_row_with_no_recorded_namespace_still_infers(
        self, db_path, tmp_path,
    ):
        """The pre-column population keeps the old answer."""
        from istota.commands import _clear_pin_across_namespaces
        config = _config(db_path, tmp_path, room_selectable=[])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.set_room_brain(conn, "room1", "native")
            conn.execute(
                "UPDATE rooms SET model = ? WHERE token = ?",
                ("z-ai/glm-5.3-flash", "room1"),
            )
            room = db.get_room(conn, "room1")
            assert room.model_namespace is None
            cleared = _clear_pin_across_namespaces(
                config, conn, "room1", room,
                source_type="talk", outgoing="native", incoming="claude_code",
            )
        assert cleared != []


class TestTheWebProducerRecordsIt:
    """`web_app._chat_update_room` is the producer ISSUE-421(c) is about — a
    model set from web chat, read on Talk — so its three namespace branches are
    covered here rather than left to the room command's."""

    def _web(self, monkeypatch, db_path, tmp_path, **kw):
        """`web_app._config` is a module global, so it is restored rather than
        assigned — a test that leaves it pointing at its own tmp_path config
        changes what every later test in the same xdist worker reads."""
        from istota import web_app
        monkeypatch.setattr(web_app, "_config", _config(db_path, tmp_path, **kw))
        return web_app

    def test_a_brain_and_a_model_in_one_body_record_the_incoming_brain(
        self, monkeypatch, db_path, tmp_path,
    ):
        """The namespace is derived after `set_room_brain`, so it describes the
        brain the route validated the model against — not the outgoing one."""
        web_app = self._web(
            monkeypatch, db_path, tmp_path,
            kind="claude_code", room_selectable=["native"],
        )
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "general")
        web_app._chat_update_room(
            "alice", room.id, None, None,
            model="z-ai/glm-5.3-flash", brain="native",
        )
        with db.get_db(db_path) as conn:
            reg = db.get_room(conn, room.token)
        assert reg.brain == "native"
        assert reg.model == "z-ai/glm-5.3-flash"
        assert reg.model_namespace == "openai_compat"

    def test_an_effort_only_save_carries_the_stored_namespace_through(
        self, monkeypatch, db_path, tmp_path,
    ):
        """The branch that must NOT re-derive: the model is unchanged, so
        stamping today's lane onto it is the inference this column replaces."""
        web_app = self._web(monkeypatch, db_path, tmp_path)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "general")
            db.set_room_model_effort(
                conn, room.token, "some-id", None, namespace="openai_compat",
            )
        web_app._chat_update_room("alice", room.id, None, None, effort="high")
        with db.get_db(db_path) as conn:
            reg = db.get_room(conn, room.token)
        assert reg.model == "some-id"
        assert reg.effort == "high"
        assert reg.model_namespace == "openai_compat"

    def test_clearing_the_model_clears_the_namespace(
        self, monkeypatch, db_path, tmp_path,
    ):
        web_app = self._web(monkeypatch, db_path, tmp_path)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "general")
            db.set_room_model_effort(
                conn, room.token, "some-id", "high", namespace="anthropic",
            )
        web_app._chat_update_room("alice", room.id, None, None, model=None)
        with db.get_db(db_path) as conn:
            reg = db.get_room(conn, room.token)
        assert reg.model is None
        assert reg.model_namespace is None


class TestTheSubtaskInheritanceReadsTheFactToo:
    """Where ISSUE-420's column and ISSUE-421(b)'s producer fix meet.

    `_inherited_model` drops a parent's model when the parent's lane and the
    `subtask` lane resolve to different namespaces. It inferred the parent's
    from `tasks.source_type`, which is a third reader of the inference this
    column replaces — so a room turn whose namespace *was* recorded lost its pin
    on a lane-routed deployment even though the recorded fact proves parent and
    child agree. Neither change produces this on its own; it appears only once
    both are in the tree, which is why it is pinned here.
    """

    def _config_routed(self, db_path, tmp_path):
        return _config(
            db_path, tmp_path, kind="claude_code",
            source_type_overrides={"talk": "native"},
        )

    def test_a_recorded_namespace_that_matches_the_child_keeps_the_model(
        self, db_path, tmp_path,
    ):
        """Parent is a `talk` turn routed to native, but its model was recorded
        as `anthropic` — the room's pin was refused, so the alias resolved in
        the lane. The child's `subtask` lane is unrouted, so also `anthropic`.
        They agree on the recorded fact and the model must carry."""
        from istota.scheduler_deferred import _inherited_model
        config = self._config_routed(db_path, tmp_path)
        parent = _Task(
            "claude-opus-5", source_type="talk", model_namespace="anthropic",
        )
        model, dropped = _inherited_model(parent, config)
        assert model == "claude-opus-5"
        assert dropped is None

    def test_a_recorded_namespace_that_differs_still_drops(
        self, db_path, tmp_path,
    ):
        """The fact is an origin, not an exemption."""
        from istota.scheduler_deferred import _inherited_model
        config = self._config_routed(db_path, tmp_path)
        parent = _Task(
            "z-ai/glm-5.3-flash", source_type="web",
            model_namespace="openai_compat",
        )
        model, dropped = _inherited_model(parent, config)
        assert model is None
        assert dropped is not None

    def test_an_unrecorded_parent_still_infers_from_the_lane(
        self, db_path, tmp_path,
    ):
        """The pre-column population keeps ISSUE-421(b)'s answer."""
        from istota.scheduler_deferred import _inherited_model
        config = self._config_routed(db_path, tmp_path)
        parent = _Task("claude-opus-5", source_type="talk")
        model, dropped = _inherited_model(parent, config)
        assert model is None
        assert dropped is not None
