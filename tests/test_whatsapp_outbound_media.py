"""Outbound WhatsApp media: an image the model embedded goes out as an image.

The model declares a picture the way it already does in web chat, a markdown
image of a `/chat/files` URL. The WhatsApp send lifts the first one out of the
text, stages a metadata-free copy in the media directory and hands the sidecar
its name, under the same single ledger claim the text would have had. These
cases hold the three properties that matter: the file is the owner's and only
the owner's, nothing about the original (EXIF, a GPS fix) leaves with it, and
one logical send is still one claim and one call.
"""

from __future__ import annotations

import asyncio
import io
import os
from urllib.parse import quote

import pytest
from PIL import Image

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import WhatsAppTransport
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp import media as media_rules
from istota.transport.whatsapp import outbound
from istota.transport.whatsapp import outbound_media
from istota.transport.whatsapp._types import (
    WhatsAppOutboundMedia,
    WhatsAppSendRequest,
    WhatsAppSendResult,
)

from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
USER_NUMBER = "+15551234567"
USER_JID = "15551234567@s.whatsapp.net"


def _config(tmp_path, *, provider="baileys") -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    workspace = tmp_path / "workspace"
    (workspace / "Users" / USER / "istota").mkdir(parents=True)
    (workspace / "Users" / "bob" / "istota").mkdir(parents=True)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        workspace_path=workspace,
        users={USER: UserConfig(), "bob": UserConfig()},
        whatsapp=build_whatsapp_config(
            enabled=True,
            provider=provider,
            waba_id="123456789012345",
            phone_number_id="223456789012345",
            business_phone_number="+15551230000",
            access_token="wa-access-token",
            app_secret="wa-app-secret",
            verify_token="wa-verify-token",
            business_timezone="UTC",
        ),
    )


def _bind(config):
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(conn, USER, bootstrap_phone_number=USER_NUMBER)
        db.latch_whatsapp_jid(conn, USER, jid=USER_JID)


def _home(config, user=USER):
    return config.workspace_path / "Users" / user / "istota"


def _png(path, size=(40, 30), *, text_chunk: str | None = None):
    image = Image.new("RGB", size, (200, 30, 30))
    kwargs = {}
    if text_chunk is not None:
        from PIL import PngImagePlugin

        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", text_chunk)
        kwargs["pnginfo"] = info
    image.save(path, "PNG", **kwargs)
    return path


def _jpeg_with_gps(path):
    image = Image.new("RGB", (64, 48), (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {2: (51.0, 30.0, 0.0), 1: "N"}  # GPSInfo
    image.save(path, "JPEG", exif=exif.tobytes())
    return path


def _link(user, relative, alt="a picture"):
    encoded = quote(f"/Users/{user}/istota/{relative}", safe="")
    return f"![{alt}](/istota/api/chat/files?path={encoded})"


class _Adapter:
    """The coarse fake, at the adapter boundary, recording what it was sent and
    whether the staged file was there to send when it was."""

    def __init__(self, config):
        self.config = config
        self.requests: list[WhatsAppSendRequest] = []
        self.staged_bytes: list[bytes | None] = []

    async def send(self, request):
        self.requests.append(request)
        if request.media is not None:
            path = media_rules.default_media_dir(self.config) / request.media.name
            self.staged_bytes.append(path.read_bytes() if path.exists() else None)
        else:
            self.staged_bytes.append(None)
        return WhatsAppSendResult(f"wamid.{len(self.requests)}")


def _deliver(config, adapter, text, **kwargs):
    kwargs.setdefault("logical_key", "task-result:1")
    return asyncio.run(outbound.deliver_whatsapp(
        config, user_id=USER, text=text, client=adapter, **kwargs,
    ))


# ---------------------------------------------------------------------------
# Lifting the image out of the text
# ---------------------------------------------------------------------------


class TestSplittingTheText:
    def test_the_first_image_is_lifted_and_its_alt_text_kept_for_the_fallback(self):
        text = "Here it is:\n" + _link(USER, "meme.png", "Distracted cat") + "\nEnjoy."

        split = outbound_media.split_image(text)

        assert split.path == f"/Users/{USER}/istota/meme.png"
        assert split.with_alt == "Here it is:\nDistracted cat\nEnjoy."
        assert split.without == "Here it is:\n\nEnjoy."

    def test_a_second_image_becomes_its_alt_text_in_both(self):
        text = _link(USER, "a.png", "first") + " and " + _link(USER, "b.png", "second")

        split = outbound_media.split_image(text)

        assert split.path.endswith("/a.png")
        assert split.with_alt == "first and second"
        assert split.without == "and second"

    def test_a_plain_link_and_a_foreign_image_are_left_alone(self):
        text = (
            "[report](/istota/api/chat/files?path=%2FUsers%2Falice%2Fr.csv) "
            "![x](https://example.com/x.png)"
        )

        split = outbound_media.split_image(text)

        assert split.path is None
        assert split.with_alt == text
        assert split.without == text

    def test_an_image_inside_code_is_not_an_image(self):
        text = "`" + _link(USER, "a.png") + "`"

        assert outbound_media.split_image(text).path is None


# ---------------------------------------------------------------------------
# Staging the copy
# ---------------------------------------------------------------------------


class TestStagingTheCopy:
    def test_a_png_is_staged_private_and_without_its_text_chunks(self, tmp_path):
        config = _config(tmp_path)
        _png(_home(config) / "meme.png", text_chunk="secret note")

        staged = outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/meme.png",
        )

        assert staged is not None
        assert staged.kind == "image" and staged.mimetype == "image/png"
        path = media_rules.default_media_dir(config) / staged.name
        assert media_rules.is_staged_name(staged.name)
        assert oct(path.stat().st_mode & 0o777) == oct(media_rules.MEDIA_FILE_MODE)
        assert b"secret note" not in path.read_bytes()
        with Image.open(path) as image:
            assert image.size == (40, 30)

    def test_a_jpeg_loses_its_exif_and_its_gps_fix(self, tmp_path):
        config = _config(tmp_path)
        _jpeg_with_gps(_home(config) / "photo.jpg")

        staged = outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/photo.jpg",
        )

        assert staged is not None and staged.mimetype == "image/jpeg"
        data = (media_rules.default_media_dir(config) / staged.name).read_bytes()
        assert b"SecretCam" not in data
        with Image.open(io.BytesIO(data)) as image:
            assert not image.getexif()

    def test_a_large_image_is_scaled_to_the_outbound_edge(self, tmp_path):
        config = _config(tmp_path)
        _png(_home(config) / "big.png", size=(4000, 1000))

        staged = outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/big.png",
        )

        with Image.open(media_rules.default_media_dir(config) / staged.name) as image:
            assert max(image.size) == outbound_media.OUTBOUND_MAX_EDGE

    def test_a_rotated_photo_is_sent_upright(self, tmp_path):
        config = _config(tmp_path)
        image = Image.new("RGB", (60, 20), (10, 120, 200))
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90 CW to display
        image.save(_home(config) / "turned.jpg", "JPEG", exif=exif.tobytes())

        staged = outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/turned.jpg",
        )

        with Image.open(media_rules.default_media_dir(config) / staged.name) as out:
            assert out.size == (20, 60)

    def test_a_png_over_the_byte_bound_goes_as_jpeg(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        noise = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
        noise.save(_home(config) / "noise.png", "PNG")
        monkeypatch.setattr(outbound_media, "OUTBOUND_MAX_BYTES", 100_000)

        staged = outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/noise.png",
        )

        assert staged is not None and staged.mimetype == "image/jpeg"
        size = (media_rules.default_media_dir(config) / staged.name).stat().st_size
        assert size <= 100_000

    def test_a_transparent_png_over_the_bound_is_not_sent(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        noise = Image.frombytes("RGBA", (200, 200), os.urandom(200 * 200 * 4))
        noise.save(_home(config) / "noise.png", "PNG")
        monkeypatch.setattr(outbound_media, "OUTBOUND_MAX_BYTES", 100_000)

        assert outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/noise.png",
        ) is None

    def test_another_users_file_is_refused(self, tmp_path):
        config = _config(tmp_path)
        _png(_home(config, "bob") / "private.png")

        assert outbound_media.stage_image(
            config, USER, "/Users/bob/istota/private.png",
        ) is None

    def test_a_symlink_out_of_the_workspace_is_refused(self, tmp_path):
        config = _config(tmp_path)
        outside = _png(tmp_path / "outside.png")
        os.symlink(outside, _home(config) / "link.png")

        assert outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/link.png",
        ) is None

    def test_an_svg_named_png_is_refused(self, tmp_path):
        config = _config(tmp_path)
        (_home(config) / "fake.png").write_text(
            "<svg xmlns='http://www.w3.org/2000/svg'/>"
        )

        assert outbound_media.stage_image(
            config, USER, f"/Users/{USER}/istota/fake.png",
        ) is None
        assert list(media_rules.default_media_dir(config).glob("*")) == []


# ---------------------------------------------------------------------------
# Through the ledger
# ---------------------------------------------------------------------------


class TestThroughTheLedger:
    def test_one_claim_one_call_and_the_image_as_native_media(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        record = _deliver(
            config, adapter, "Here:\n" + _link(USER, "meme.png", "cat"),
            attach_media=True,
        )

        assert record.status == "accepted"
        assert len(adapter.requests) == 1
        request = adapter.requests[0]
        assert request.media is not None
        assert request.media.mimetype == "image/png"
        assert request.media.caption == "Here:"
        # What a sidecar that cannot read the file sends instead.
        assert request.text == "Here:\ncat"
        assert adapter.staged_bytes[0] is not None
        # The staged copy is gone once the send settled.
        assert not (media_rules.default_media_dir(config) / request.media.name).exists()

    def test_a_retry_on_the_same_key_sends_nothing_and_stages_nothing(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)
        text = _link(USER, "meme.png", "cat")

        _deliver(config, adapter, text, attach_media=True)
        _deliver(config, adapter, text, attach_media=True)

        assert len(adapter.requests) == 1
        assert list(media_rules.default_media_dir(config).glob("*")) == []

    def test_an_unreadable_image_sends_the_text_with_its_alt(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        adapter = _Adapter(config)

        record = _deliver(
            config, adapter, "Look: " + _link(USER, "missing.png", "the chart"),
            attach_media=True,
        )

        assert record.status == "accepted"
        assert adapter.requests[0].media is None
        assert adapter.requests[0].text == "Look: the chart"

    def test_without_the_opt_in_the_link_becomes_its_alt_text(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        _deliver(config, adapter, "Look: " + _link(USER, "meme.png", "the cat"))

        assert adapter.requests[0].media is None
        assert adapter.requests[0].text == "Look: the cat"

    def test_a_question_with_buttons_never_carries_media(self, tmp_path):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        _deliver(
            config, adapter, "Send this? " + _link(USER, "meme.png", "the cat"),
            attach_media=True, buttons=(("c:1:yes", "Yes"), ("c:1:no", "No")),
        )

        assert adapter.requests[0].media is None

    def test_a_provider_without_outbound_media_gets_the_alt_text(self, tmp_path):
        config = _config(tmp_path, provider="whatsapp_cloud")
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(
                conn, USER, bootstrap_phone_number=USER_NUMBER, bsuid="US.1",
            )
            conn.execute(
                "UPDATE whatsapp_user_bindings SET send_id='US.1', "
                "last_user_message_at=datetime('now') WHERE user_id=?", (USER,),
            )
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        _deliver(config, adapter, _link(USER, "meme.png", "the cat"), attach_media=True)

        assert adapter.requests[0].media is None
        assert adapter.requests[0].text == "the cat"


# ---------------------------------------------------------------------------
# Who may attach: the transport's own decision
# ---------------------------------------------------------------------------


def _task(config, **fields):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt="share a meme", user_id=USER, source_type="whatsapp",
        )
        for name, value in fields.items():
            conn.execute(f"UPDATE tasks SET {name} = ? WHERE id = ?", (value, task_id))
        return db.get_task(conn, task_id)


class TestTheTransportDecides:
    def _send(self, config, task, adapter, monkeypatch, reference_id=None):
        monkeypatch.setattr(outbound, "active_adapter", lambda c: _with(c, adapter))
        return asyncio.run(WhatsAppTransport(config).send_record(
            "", "Here: " + _link(USER, "meme.png", "cat"), task=task,
            reference_id=reference_id or f"task-result:{task.id}",
        ))

    def test_a_task_result_attaches(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        self._send(config, _task(config), adapter, monkeypatch)

        assert adapter.requests[0].media is not None

    def test_a_guest_turn_never_attaches_the_hosts_file(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)

        self._send(
            config, _task(config, guest_participant_id=7), adapter, monkeypatch,
        )

        assert adapter.requests[0].media is None
        assert adapter.requests[0].text == "Here: cat"

    def test_a_confirmation_prompt_never_attaches(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        _bind(config)
        _png(_home(config) / "meme.png")
        adapter = _Adapter(config)
        task = _task(config)

        self._send(
            config, task, adapter, monkeypatch,
            reference_id=f"confirmation-task:{task.id}",
        )

        assert adapter.requests[0].media is None


def _with(config, adapter):
    from dataclasses import replace

    from istota.transport.whatsapp.providers.registry import make_provider_registry

    real = make_provider_registry(config).active()
    return replace(real, send=adapter.send)


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


class TestTheWire:
    def test_a_media_part_crosses_as_name_mimetype_and_kind(self):
        request = WhatsAppSendRequest(
            to=USER_JID, text="caption", kind="service",
            media=WhatsAppOutboundMedia(
                name="out-0123456789abcdef.png", mimetype="image/png", kind="image",
                caption="",
            ),
        )

        payload = proto.send_payload("r1", request)

        assert payload["media"] == {
            "name": "out-0123456789abcdef.png",
            "mimetype": "image/png",
            "kind": "image",
            "caption": "",
        }
        assert proto.decode(proto.encode(proto.MSG_SEND, **payload))["media"] == (
            payload["media"]
        )

    def test_a_text_send_carries_no_media_field(self):
        request = WhatsAppSendRequest(to=USER_JID, text="hi", kind="service")

        assert "media" not in proto.send_payload("r1", request)

    def test_only_baileys_declares_outbound_media(self):
        from istota.transport.whatsapp.providers.baileys import BAILEYS_CAPS
        from istota.transport.whatsapp.providers.whatsapp_cloud import CLOUD_CAPS

        assert BAILEYS_CAPS.outbound_media is True
        assert CLOUD_CAPS.outbound_media is False


# ---------------------------------------------------------------------------
# The skill's --file
# ---------------------------------------------------------------------------


class TestTheSelfSendFile:
    def test_a_workspace_file_becomes_an_embedded_image(self, tmp_path, monkeypatch):
        from istota.skills import whatsapp as skill

        config = _config(tmp_path)
        path = _png(_home(config) / "chart.png")
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(config.workspace_path))
        monkeypatch.setenv("ISTOTA_USER_ID", USER)

        text = skill.with_file("Your chart", str(path))

        assert text == "Your chart\n\n" + _link(USER, "chart.png", "chart.png")
        assert outbound_media.split_image(text).path == (
            f"/Users/{USER}/istota/chart.png"
        )

    def test_an_awkward_name_survives_the_renderer(self, tmp_path, monkeypatch):
        """A `]` would end the label and a `__` is rewritten by the WhatsApp
        renderer, which a self-send's stored body goes through."""
        from istota.skills import whatsapp as skill

        config = _config(tmp_path)
        path = _png(_home(config) / "a]b__v2.png")
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(config.workspace_path))
        monkeypatch.setenv("ISTOTA_USER_ID", USER)

        rendered = outbound.render_whatsapp(skill.with_file("x", str(path)))

        assert outbound_media.split_image(rendered).path == (
            f"/Users/{USER}/istota/a]b__v2.png"
        )

    def test_a_file_outside_the_workspace_is_refused(self, tmp_path, monkeypatch):
        from istota.relay.requests import RequestError
        from istota.skills import whatsapp as skill

        config = _config(tmp_path)
        other = tmp_path / "tmp-deferred" / "x.png"
        other.parent.mkdir()
        _png(other)
        monkeypatch.setenv("ISTOTA_WORKSPACE_PATH", str(config.workspace_path))
        monkeypatch.setenv("ISTOTA_USER_ID", USER)

        with pytest.raises(RequestError, match="file_not_in_workspace"):
            skill.with_file("x", str(other))
