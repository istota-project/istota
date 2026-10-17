"""A location share's map as a picture on Talk and WhatsApp, and its image_state ledger."""

import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from PIL import Image

from istota import confirmations, db
from istota.browser.document_render import DocumentRender
from istota.location import db as location_db, map_delivery, sharing
from istota.location.logic import share_map_url
from istota.nextcloud import talk_files
from istota.relay import requests
from istota.transport.whatsapp import outbound
from istota.transport.whatsapp import media as media_rules
from istota.transport.whatsapp.outbound_media import stage_image_bytes
from istota.transport.whatsapp.providers._types import WhatsAppProviderAdapter, WhatsAppProviderCaps
from istota.webui import app

from .support.rooms import plain_talk_room
from .support.talk_double import FakeTalkClient, talk_bot_client
from .test_nextcloud_talk_group import _DavRecorder
from .test_private_replies import _config, sent as _sent_fixture
from .test_whatsapp_groups import ALICE_JID, BOB_JID, GROUP, _apply, _message, _roster

sent = _sent_fixture

URL = share_map_url("exact", 12.345678, -45.678912)
OBSERVATION = {
    "owner_user_id": "alice", "owner_label": "Alice", "precision": "exact",
    "lat": 12.345678, "lon": -45.678912, "accuracy_m": 10.0,
    "observed_at": "2026-10-16T14:02:00+00:00", "freshness": "fresh", "map_url": URL,
}


def _png(width=640, height=360) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 160, 200)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.workspace_path = tmp_path / "files"
    config.talk.bot_username = "istota"
    monkeypatch.setattr(app, "_config", config)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    return config


@pytest.fixture
def renders(monkeypatch):
    """The browser's answer, at the document-render seam. Append a DocumentRender to change it."""
    answers = []
    calls = []

    def render_document(config, **kwargs):
        calls.append(kwargs)
        return answers.pop(0) if answers else DocumentRender(_png(), None)

    monkeypatch.setattr("istota.location.share_maps.render_document", render_document)
    return answers, calls


@pytest.fixture
def dav(monkeypatch):
    recorder = _DavRecorder()
    monkeypatch.setattr("istota.nextcloud._http.httpx.request", recorder)
    return recorder


@pytest.fixture
def talk(config, monkeypatch):
    double = FakeTalkClient(config.db_path)
    monkeypatch.setattr("istota.transport.talk.get_talk_client", talk_bot_client(double))
    return double


def _talk_room(config):
    with db.get_db(config.db_path) as conn:
        shape = plain_talk_room(conn, "alice", name="Hiking")
        db.add_room_member(conn, shape.canonical, "bob")
    return shape


def _attached_card(config, room, *, observation=OBSERVATION, body=None):
    """One card recorded and attached to an assistant row in ``room``, as final delivery leaves it."""
    with db.get_db(config.db_path) as conn:
        card_id = sharing._insert_card(conn, task_id=None, actor_user_id="alice",
                                       room_token=room, observation=observation)
        message_id = db.add_message(
            conn, room, role="assistant", body=body or f"Here: {observation['map_url']}",
            origin_surface="talk", location_shares=[{"card_id": card_id, **observation}])
        sharing.mark_location_cards_attached(conn, [card_id], message_id)
    return card_id, message_id


def _card(config, card_id):
    with db.get_db(config.db_path) as conn:
        return dict(conn.execute("SELECT * FROM location_share_cards WHERE id=?",
                                 (card_id,)).fetchone())


def _shares(double):
    return [call for call in double.calls if call.method == "share_file" and not call.refused]


# --- the extracted Talk upload ---------------------------------------------


def test_upload_copy_puts_into_the_days_talk_folder_and_numbers_a_taken_name(config, dav, tmp_path):
    source = tmp_path / "map.png"
    source.write_bytes(_png())
    folder = talk_files.talk_folder("abc123")
    dav.existing.add(f"/remote.php/dav/files/bot/{folder}/map.png")

    remote = talk_files.upload_copy(config, "abc123", source, "map.png")

    assert remote == f"/{folder}/map (2).png"
    puts = dav.puts()
    assert [path for path, _ in puts][-1] == f"/remote.php/dav/files/bot{remote}"
    assert all(headers.get("If-None-Match") == "*" for _, headers in puts)


def test_upload_copy_raises_when_every_name_is_taken(config, dav, tmp_path, monkeypatch):
    source = tmp_path / "map.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(talk_files, "MAX_UPLOAD_NAMES", 2)
    folder = talk_files.talk_folder("abc123")
    dav.existing.update({f"/remote.php/dav/files/bot/{folder}/map.png",
                         f"/remote.php/dav/files/bot/{folder}/map (2).png"})
    with pytest.raises(talk_files.NamesExhausted):
        talk_files.upload_copy(config, "abc123", source, "map.png")


# --- WhatsApp staging and the send ------------------------------------------


def test_stage_image_bytes_stages_a_clean_copy_and_refuses_what_is_not_an_image(config):
    staged = stage_image_bytes(config, _png())
    path = media_rules.default_media_dir(config) / staged.name
    assert staged.mimetype == "image/png" and media_rules.is_staged_name(staged.name)
    assert Image.open(path).size == (640, 360)
    assert stage_image_bytes(config, b"<svg/>") is None


def test_a_generated_image_rides_the_send_as_its_caption_and_takes_the_slot(config, sent):
    body = "Alice is here ![chart](/istota/api/chat/files?path=%2FUsers%2Falice%2Fchart.png)"
    record = asyncio.run(outbound.deliver_whatsapp(
        config, logical_key="k1", user_id="alice", text=body, attach_media=True,
        generated_image=_png()))
    (request,) = sent
    assert record.status == "accepted" and record.media_staged
    assert request.media.mimetype == "image/png"
    assert request.media.caption == request.text == "Alice is here chart"


def test_no_generated_image_where_the_adapter_cannot_send_media(config, monkeypatch):
    seen = []

    async def send(request):
        seen.append(request)
        from istota.transport.whatsapp._types import WhatsAppSendResult
        return WhatsAppSendResult(message_id="M")

    caps = WhatsAppProviderCaps(
        metered=False, has_service_window=False, supports_templates=False,
        delivery_receipts=True, address_field="jid", service_body_limit=4096,
        interactive_body_limit=4096, outbound_media=False)
    adapter = WhatsAppProviderAdapter(name="baileys", caps=caps, parse_webhook=None,
                                      send=send, verify_signature=None)
    monkeypatch.setattr(outbound, "active_adapter", lambda config: adapter)
    record = asyncio.run(outbound.deliver_whatsapp(
        config, logical_key="k2", user_id="alice", text="hi", generated_image=_png()))
    assert seen[0].media is None and not record.media_staged
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "x").token
    card_id, message_id = _attached_card(config, room)
    assert map_delivery.prepare_whatsapp_map(config, message_id=message_id, room_token=room) is None
    assert _card(config, card_id)["image_state"] == "none"


# --- the image_state ledger, on Talk ----------------------------------------


def test_talk_map_is_uploaded_and_shared_once_and_never_again(config, renders, dav, talk):
    shape = _talk_room(config)
    card_id, message_id = _attached_card(config, shape.canonical)

    states = map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref)

    assert states == ["sent"]
    (share,) = _shares(talk)
    assert share.token == shape.talk_ref
    assert share.args["path"].startswith(f"/Talk/{shape.talk_ref}/") and share.args["path"].endswith("/map.png")
    card = _card(config, card_id)
    assert card["image_state"] == "sent" and card["image_surface"] == "talk"
    assert renders[1][0]["actor_user_id"] == "alice"
    assert not list((config.temp_dir / ".share-maps").iterdir())
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == []
    assert len(_shares(talk)) == 1 and len(renders[1]) == 1


@pytest.mark.parametrize("answer, state", [
    (DocumentRender(None, "render_unavailable"), "skipped"),
    (DocumentRender(None, "render_timeout"), "failed"),
    (DocumentRender(_png(320, 200), None), "failed"),
])
def test_no_picture_records_skipped_or_failed_and_shares_nothing(config, renders, dav, talk, answer, state):
    shape = _talk_room(config)
    card_id, message_id = _attached_card(config, shape.canonical)
    renders[0].append(answer)
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == [state]
    assert _card(config, card_id)["image_state"] == state
    assert _shares(talk) == [] and dav.puts() == []


def test_a_latitude_mercator_cannot_draw_is_skipped_without_a_render(config, renders, dav, talk):
    shape = _talk_room(config)
    north = dict(OBSERVATION, lat=86.0, map_url=share_map_url("exact", 86.0, -45.678912))
    card_id, message_id = _attached_card(config, shape.canonical, observation=north)
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == ["skipped"]
    assert renders[1] == []


def test_an_unanswered_share_is_uncertain_and_never_retried(config, renders, dav, talk):
    shape = _talk_room(config)
    card_id, message_id = _attached_card(config, shape.canonical)
    talk.share_failures[shape.talk_ref] = httpx.ReadTimeout("slow")
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == ["uncertain"]
    talk.share_failures.clear()
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == []
    assert len(_shares(talk)) == 1


def test_a_refused_share_or_upload_is_failed(config, renders, dav, talk):
    shape = _talk_room(config)
    card_id, message_id = _attached_card(config, shape.canonical)
    request = httpx.Request("POST", "https://cloud.example.com/ocs")
    talk.share_failures[shape.talk_ref] = httpx.HTTPStatusError(
        "no", request=request, response=httpx.Response(403, request=request))
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == ["failed"]


def test_a_card_from_another_room_is_never_posted_here(config, renders, dav, talk):
    shape = _talk_room(config)
    with db.get_db(config.db_path) as conn:
        elsewhere = db.create_web_chat_room(conn, "alice", "Other").token
    card_id, message_id = _attached_card(config, elsewhere)
    assert map_delivery.deliver_talk_maps(config, message_id=message_id, talk_token=shape.talk_ref) == []
    assert _card(config, card_id)["image_state"] == "none" and _shares(talk) == []


# --- through the scheduler's final delivery ---------------------------------


def _alice_pings(config):
    path = config.module_db_path("alice", "location")
    location_db.init_db(path)
    with location_db.connect(path) as conn:
        location_db.insert_ping(conn, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                                lat=12.345678, lon=-45.678912, accuracy=10, wifi="private-network")
        conn.commit()


def _run_share(config, ident, *, answer=lambda snapshot: f"Here I am: {snapshot['map_url']}"):
    from istota.scheduler import process_one_task

    def execute(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            result = sharing.share_location_snapshot(
                conn, config, actor_user_id="alice", task_id=ident, request_key="once",
                now=datetime.now(timezone.utc))
        return True, answer(result["snapshot"]), None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (ident, True)


def test_a_talk_answer_carrying_the_link_posts_the_text_then_the_map(config, renders, dav, talk):
    from istota.transport.talk import inbound
    from .test_talk_trigger_path import _client, _msg

    _alice_pings(config)
    shape = _talk_room(config)
    with db.get_db(config.db_path) as conn:
        db.set_talk_poll_state(conn, shape.talk_ref, 50)
    inbound._participant_cache.clear()
    message = _msg(msg_id=101, message="{mention-user0} share my location here",
                   params={"mention-user0": {"type": "user", "id": "istota", "name": "Istota"}})
    message["actorId"] = "alice"
    with patch("istota.transport.talk.inbound.get_talk_client", return_value=_client([message])):
        (ident,) = asyncio.run(inbound.poll_one_conversation(
            config, shape.talk_ref, conv_type=2, display_name="Hiking"))
    inbound._participant_cache.clear()

    _run_share(config, ident)

    methods = [call.method for call in talk.calls if call.token == shape.talk_ref and not call.refused]
    assert methods.index("send_message") < methods.index("share_file")
    assert not talk.refusals
    with db.get_db(config.db_path) as conn:
        (card,) = [dict(row) for row in conn.execute("SELECT * FROM location_share_cards")]
    assert card["image_state"] == "sent" and card["image_surface"] == "talk" and card["message_id"]


def test_a_talk_answer_without_the_link_gets_no_map(config, renders, dav, talk):
    from istota.transport.talk import inbound
    from .test_talk_trigger_path import _client, _msg

    _alice_pings(config)
    shape = _talk_room(config)
    with db.get_db(config.db_path) as conn:
        db.set_talk_poll_state(conn, shape.talk_ref, 50)
    inbound._participant_cache.clear()
    message = _msg(msg_id=101, message="{mention-user0} share my location here",
                   params={"mention-user0": {"type": "user", "id": "istota", "name": "Istota"}})
    message["actorId"] = "alice"
    with patch("istota.transport.talk.inbound.get_talk_client", return_value=_client([message])):
        (ident,) = asyncio.run(inbound.poll_one_conversation(
            config, shape.talk_ref, conv_type=2, display_name="Hiking"))
    inbound._participant_cache.clear()

    _run_share(config, ident, answer=lambda snapshot: "You are near the river.")

    assert _shares(talk) == [] and renders[1] == []


def test_a_whatsapp_group_answer_carries_the_map_as_its_image(config, renders, sent):
    _alice_pings(config)
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
        db.latch_whatsapp_jid(conn, "bob", jid=BOB_JID)
    _apply(config, _roster([ALICE_JID, BOB_JID], added_by=ALICE_JID, subject="Hiking"))
    (result,) = _apply(config, _message("Istota, share my location here", message_id="M9"))
    assert result.disposition == "task"

    _run_share(config, result.task_id)

    (request,) = [r for r in sent if r.to == GROUP]
    assert request.media is not None and request.media.mimetype == "image/png"
    assert URL in request.media.caption
    with db.get_db(config.db_path) as conn:
        card = conn.execute("SELECT image_state, image_surface FROM location_share_cards").fetchone()
    assert tuple(card) == ("sent", "whatsapp")


# --- an approved post, released ---------------------------------------------


def _held_talk_post(config):
    """Alice's private web room holds a location post for the Talk-bound shared room."""
    shape = _talk_room(config)
    _held_post(config, shape.canonical)
    return shape


def _held_post(config, target):
    """Alice's private web room holds an approved location post for ``target``."""
    from istota.rooms import private_replies

    with db.get_db(config.db_path) as conn:
        private = db.create_web_chat_room(conn, "alice", "Private").token
        ident = db.create_task(conn, user_id="alice", source_type="web", prompt="Share my location",
                               conversation_token=private, about_room_token=target)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        db.add_message(conn, private, role="user", body="Share my location", origin_surface="web",
                       task_id=ident, author_user_id="alice")
        text = f"{sharing.observation_line(OBSERVATION)}\n{URL}"
        private_replies.hold_room_post(conn, config, actor_user_id="alice", task_id=ident,
                                       request_key="p1", text=text, attachment=OBSERVATION,
                                       require_confirmation=True)
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")


def _drain(config):
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))


def test_a_released_post_shares_its_map_after_the_text(config, renders, dav, talk):
    shape = _held_talk_post(config)
    _drain(config)
    methods = [call.method for call in talk.calls if call.token == shape.talk_ref and not call.refused]
    assert methods == ["send_message", "share_file"]
    with db.get_db(config.db_path) as conn:
        card = conn.execute("SELECT image_state, request_id FROM location_share_cards").fetchone()
        assert card["image_state"] == "sent" and card["request_id"]
        assert conn.execute("SELECT count(*) FROM notifications WHERE dedup_key LIKE 'private-reply-map:%'"
                            ).fetchone()[0] == 0


def _map_notices(config):
    with db.get_db(config.db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT user_id, body FROM notifications WHERE dedup_key LIKE 'private-reply-map:%'")]


def test_a_released_post_whose_map_was_skipped_tells_the_owner(config, renders, dav, talk):
    """The approval promised a map; no browser service is still no map in Talk."""
    renders[0].append(DocumentRender(None, "render_unavailable"))
    _held_talk_post(config)
    _drain(config)

    (notice,) = _map_notices(config)
    assert notice["user_id"] == "alice" and "map unavailable" in notice["body"]


def test_a_released_post_into_a_web_room_needs_no_picture_and_no_notice(config, renders):
    """Web draws the card itself, so the promised map is there without a render."""
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Hiking").token
        db.add_web_room_member(conn, room, "bob")
    renders[0].append(DocumentRender(None, "render_unavailable"))
    _held_post(config, room)
    _drain(config)

    assert renders[1] == [] and _map_notices(config) == []
    with db.get_db(config.db_path) as conn:
        (shares,) = conn.execute("SELECT location_shares FROM messages WHERE room_token=? "
                                 "AND role='assistant'", (room,)).fetchone()
    assert json.loads(shares)[0]["map_url"] == URL


def test_a_released_post_into_a_group_whose_map_failed_tells_the_owner(config, renders, sent):
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
        db.latch_whatsapp_jid(conn, "bob", jid=BOB_JID)
    _apply(config, _roster([ALICE_JID, BOB_JID], added_by=ALICE_JID, subject="Hiking"))
    with db.get_db(config.db_path) as conn:
        group = db.resolve_room_token(conn, "whatsapp", GROUP)
    renders[0].append(DocumentRender(None, "render_timeout"))
    _held_post(config, group)
    _drain(config)

    (request,) = [r for r in sent if r.to == GROUP]
    assert request.media is None
    (notice,) = _map_notices(config)
    assert notice["user_id"] == "alice"


def test_a_released_post_whose_map_failed_tells_the_owner(config, renders, dav, talk):
    renders[0].append(DocumentRender(None, "render_timeout"))
    shape = _held_talk_post(config)
    _drain(config)
    assert [call.method for call in talk.calls
            if call.token == shape.talk_ref and not call.refused] == ["send_message"]
    with db.get_db(config.db_path) as conn:
        row = conn.execute("SELECT user_id, title, body FROM notifications "
                           "WHERE dedup_key LIKE 'private-reply-map:%'").fetchone()
        assert row["user_id"] == "alice" and "map unavailable" in row["body"]
        assert json.dumps(OBSERVATION["lat"]) not in row["body"]
        assert conn.execute("SELECT state FROM whatsapp_skill_requests").fetchone()[0] == "sent"
