"""GIFs on WhatsApp, decoded into one tiled still (ISSUE-647).

WhatsApp sends a GIF as an MP4 `videoMessage` with `gifPlayback`. The child
(`lib/gif_frames`) tiles up to four frames into one JPEG and the runner
(`transport/whatsapp/gif_frames`) spawns it with a deadline. The real decode
needs PyAV, which ships with the `whisper` extra, so those cases carry `ml`.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from istota.lib import gif_frames as leaf
from istota.lib.video_sniff import sniff_video
from istota.transport.whatsapp import gif_frames as runner
from istota.transport.whatsapp import media
from istota.transport.whatsapp.baileys_bridge import stage_inbound_media
from istota.transport.whatsapp.webhook import GIF_FRAMES_NOTE, GIF_ONLY_PROMPT

from . import test_whatsapp_media_precheck as staging
from . import test_whatsapp_webhook as hooks


class TestWhichFramesAreKept:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, []), (1, [0]), (3, [0, 1, 2]), (4, [0, 1, 2, 3]),
         (10, [0, 3, 6, 9]), (100, [0, 33, 66, 99])],
    )
    def test_spread_evenly_with_both_ends(self, count, expected):
        assert leaf.chosen_indexes(count) == expected


class TestTheSniff:
    @pytest.mark.parametrize("brand", [b"isom", b"mp42", b"avc1", b"iso5"])
    def test_an_mp4_video_brand_is_video(self, brand):
        assert sniff_video(b"\x00\x00\x00\x20ftyp" + brand + b"\x00" * 20) == "video/mp4"

    @pytest.mark.parametrize("head", [
        b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20,
        b"GIF89a" + b"\x00" * 20,
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 20,
        b"",
        None,
        "ftypisom",
    ])
    def test_anything_else_is_not(self, head):
        assert sniff_video(head) is None


class TestTheRunner:
    """The parent half, driven with stand-in children."""

    @staticmethod
    def _child(monkeypatch, script):
        monkeypatch.setattr(
            runner, "child_argv",
            lambda src, dest: [sys.executable, "-c", script],
        )

    def test_success_is_none(self, monkeypatch):
        self._child(monkeypatch, 'print(\'{"status": "ok", "frames": 4}\')')

        assert runner.extract_frames("a", "b") is None

    def test_the_childs_code_is_passed_on(self, monkeypatch):
        self._child(monkeypatch, 'print(\'{"status": "error", "error": "too_long"}\')')

        assert runner.extract_frames("a", "b") == "too_long"

    def test_an_unknown_code_is_no_result(self, monkeypatch):
        self._child(monkeypatch, 'print(\'{"status": "error", "error": "\\\\nforged"}\')')

        assert runner.extract_frames("a", "b") == "no_result"

    def test_noise_before_the_result_is_skipped(self, monkeypatch):
        self._child(monkeypatch, 'print("{oops"); print(\'{"status": "ok"}\')')

        assert runner.extract_frames("a", "b") is None

    def test_no_json_is_no_result(self, monkeypatch):
        self._child(monkeypatch, 'print("segfault")')

        assert runner.extract_frames("a", "b") == "no_result"

    def test_a_hang_is_killed_at_the_deadline(self, monkeypatch):
        self._child(monkeypatch, "import time; time.sleep(30)")

        assert runner.extract_frames("a", "b", timeout=0.5) == "timed_out"

    def test_the_spawn_names_the_leaf_and_guards_the_paths(self):
        argv = runner.child_argv("-x.mp4", "/tmp/out.jpg")

        assert argv[argv.index("-m") + 1] == "istota.lib.gif_frames"
        assert "-P" in argv
        assert argv[-3:] == ["--", "-x.mp4", "/tmp/out.jpg"]


def test_the_leaf_imports_nothing_from_istota():
    """What a spawned child imports is paid per GIF (`ocr_leaf`'s rule)."""
    probe = (
        "import sys; import istota.lib.gif_frames; "
        "print(','.join(sorted(m for m in sys.modules if m.startswith('istota.') "
        "and m not in ('istota.lib', 'istota.lib.gif_frames'))))"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", probe], capture_output=True, text=True,
        timeout=30, check=True,
    )

    assert result.stdout.strip() == ""


def _mp4(path, *, frames=12, width=64, height=48):
    av = pytest.importorskip("av")
    from PIL import Image

    with av.open(str(path), mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for i in range(frames):
            still = Image.new("RGB", (width, height), (i * 20 % 256, 80, 160))
            frame = av.VideoFrame.from_image(still)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


@pytest.mark.ml
class TestTheRealDecode:
    def test_a_short_clip_becomes_four_tiled_frames(self, tmp_path):
        from PIL import Image

        _mp4(tmp_path / "gif.mp4")
        out = tmp_path / "frames.jpg"

        assert runner.extract_frames(str(tmp_path / "gif.mp4"), str(out)) is None
        with Image.open(out) as sheet:
            assert sheet.size == (128, 96)
        assert (out.stat().st_mode & 0o777) == 0o600

    def test_one_frame_is_one_cell(self, tmp_path):
        from PIL import Image

        _mp4(tmp_path / "gif.mp4", frames=1)
        out = tmp_path / "frames.jpg"

        assert leaf.extract(str(tmp_path / "gif.mp4"), str(out))["frames"] == 1
        with Image.open(out) as sheet:
            assert sheet.size == (64, 48)

    def test_a_frame_past_the_pixel_cap_is_refused_before_decoding(
        self, tmp_path, monkeypatch,
    ):
        _mp4(tmp_path / "gif.mp4")
        monkeypatch.setattr(leaf, "MAX_FRAME_PIXELS", 64 * 48 - 1)

        assert leaf.extract(str(tmp_path / "gif.mp4"), str(tmp_path / "o.jpg")) == {
            "status": "error", "error": "too_large"}
        assert not (tmp_path / "o.jpg").exists()

    def test_a_clip_past_the_packet_cap_is_refused(self, tmp_path, monkeypatch):
        _mp4(tmp_path / "gif.mp4", frames=12)
        monkeypatch.setattr(leaf, "MAX_PACKETS", 5)

        assert leaf.extract(str(tmp_path / "gif.mp4"), str(tmp_path / "o.jpg"))[
            "error"] == "too_long"

    def test_a_truncated_file_is_unreadable_and_writes_nothing(self, tmp_path):
        _mp4(tmp_path / "gif.mp4")
        data = (tmp_path / "gif.mp4").read_bytes()
        (tmp_path / "cut.mp4").write_bytes(data[: len(data) // 3])

        code = runner.extract_frames(str(tmp_path / "cut.mp4"), str(tmp_path / "o.jpg"))

        assert code in {"unreadable", "no_frames", "no_video"}
        assert not (tmp_path / "o.jpg").exists()

    def test_an_existing_destination_is_never_overwritten(self, tmp_path):
        _mp4(tmp_path / "gif.mp4")
        (tmp_path / "o.jpg").write_bytes(b"keep")

        assert leaf.extract(str(tmp_path / "gif.mp4"), str(tmp_path / "o.jpg"))[
            "error"] == "write_failed"
        assert (tmp_path / "o.jpg").read_bytes() == b"keep"


def test_without_pyav_the_child_says_so(tmp_path):
    """The `whisper` extra is optional; its absence is a reason, not a crash."""
    probe = (
        "import sys; sys.modules['av'] = None; "
        "from istota.lib import gif_frames; "
        f"print(gif_frames.extract({str(tmp_path / 'x.mp4')!r}, "
        f"{str(tmp_path / 'o.jpg')!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        timeout=30, check=True,
    )

    assert "decoder_missing" in result.stdout


# ---------------------------------------------------------------------------
# Through the surface
# ---------------------------------------------------------------------------

MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 52


def _gif_event(name, caption=None):
    event = staging._image_event(name, caption=caption)
    return dataclasses.replace(
        event, message_type="gif",
        media=dataclasses.replace(event.media, kind="gif", mime_type="video/mp4"),
    )


def _fake_extractor(monkeypatch, code=None):
    """Stand in for the child: write a small JPEG where it would, or fail."""
    calls = []

    def extract(src, dest, *, timeout=runner.DEFAULT_TIMEOUT_SECONDS):
        calls.append((src, dest))
        if code is None:
            from PIL import Image

            Image.new("RGB", (8, 8)).save(dest, format="JPEG")
        return code

    monkeypatch.setattr(runner, "extract_frames", extract)
    return calls


class TestTheStagingStep:
    def test_a_gif_reaches_the_inbox_as_one_still_and_the_mp4_does_not(
        self, tmp_path, monkeypatch,
    ):
        config = staging._config(tmp_path)
        staging._bind_identity(config.db_path, jid=staging.USER_JID)
        name = staging._stage_a_file(config, MP4_HEAD, ext="mp4")
        calls = _fake_extractor(monkeypatch)

        staged = stage_inbound_media(config, staging._media_dir(config), _gif_event(name))

        assert len(calls) == 1
        assert staged.media.error is None
        assert staged.media.kind == "gif"
        assert staged.media.staged_path.startswith("/Users/alice/inbox/whatsapp_")
        assert staged.media.staged_path.endswith(".jpg")
        inbox = list((config.workspace_path / "Users" / "alice" / "inbox").iterdir())
        assert [p.suffix for p in inbox] == [".jpg"]
        assert list(staging._media_dir(config).iterdir()) == []

    def test_bytes_that_are_not_an_mp4_never_reach_the_decoder(
        self, tmp_path, monkeypatch,
    ):
        config = staging._config(tmp_path)
        staging._bind_identity(config.db_path, jid=staging.USER_JID)
        name = staging._stage_a_file(config, ext="mp4")  # a PNG
        calls = _fake_extractor(monkeypatch)

        staged = stage_inbound_media(config, staging._media_dir(config), _gif_event(name))

        assert calls == []
        assert staged.media is None
        assert list(staging._media_dir(config).iterdir()) == []

    @pytest.mark.parametrize("code", ["decoder_missing", "unreadable", "timed_out"])
    def test_a_gif_that_cannot_be_decoded_is_unsupported_and_leaves_nothing(
        self, tmp_path, monkeypatch, code,
    ):
        config = staging._config(tmp_path)
        staging._bind_identity(config.db_path, jid=staging.USER_JID)
        name = staging._stage_a_file(config, MP4_HEAD, ext="mp4")
        _fake_extractor(monkeypatch, code)

        staged = stage_inbound_media(config, staging._media_dir(config), _gif_event(name))

        assert staged.media is None
        assert staged.message_type == "gif"
        assert list(staging._media_dir(config).iterdir()) == []
        assert not (config.workspace_path / "Users").exists()

    def test_the_sniff_takes_a_video_for_a_gif_and_nothing_else(self, tmp_path):
        path = tmp_path / "x"
        path.write_bytes(MP4_HEAD)

        assert media.sniff_staged(path, "gif") == "video/mp4"
        assert media.sniff_staged(path, "image") is None


class TestTheTask:
    @staticmethod
    def _gif(caption):
        event = hooks._image_event(caption=caption, media=dataclasses.replace(
            hooks._media(), kind="gif",
        ))
        return dataclasses.replace(event, message_type="gif")

    def _task(self, tmp_path, caption):
        config = hooks._config(tmp_path)
        hooks._bind(config, bootstrap_phone_number=hooks.USER_NUMBER,
                    bsuid=hooks.USER_BSUID)
        (result,) = hooks._dispatch(config, self._gif(caption))
        assert result.disposition == "task"
        from istota import db

        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, result.task_id)

    def test_an_uncaptioned_gif_says_what_the_image_is(self, tmp_path):
        task = self._task(tmp_path, None)

        assert task.prompt == f"{GIF_ONLY_PROMPT}\n{GIF_FRAMES_NOTE}"
        assert task.attachments == ["/Users/alice/inbox/whatsapp_ab12-cd34.jpg"]

    def test_a_captioned_gif_keeps_the_words_first(self, tmp_path):
        task = self._task(tmp_path, "lol look")

        assert task.prompt == f"lol look\n{GIF_FRAMES_NOTE}"

    def test_a_gif_whose_fetch_failed_is_named_as_one(self, tmp_path):
        from istota.transport.whatsapp.webhook import MEDIA_FAILED_GIF_REPLY

        config = hooks._config(tmp_path)
        hooks._bind(config, bootstrap_phone_number=hooks.USER_NUMBER,
                    bsuid=hooks.USER_BSUID)
        event = hooks._image_event(caption="lol", media=dataclasses.replace(
            hooks._media(error=media.reason("gif", "fetch_failed")), kind="gif",
        ))
        (result,) = hooks._dispatch(config, dataclasses.replace(event, message_type="gif"))

        assert result.disposition == "media_failed"
        assert result.response_text == MEDIA_FAILED_GIF_REPLY


class TestTheDoctorCheck:
    def _check(self, tmp_path, monkeypatch, *, found, provider="baileys"):
        from istota import doctor

        config = staging._config(tmp_path)
        config.whatsapp.provider = provider
        monkeypatch.setattr(
            doctor.importlib.util, "find_spec",
            lambda name: object() if found else None,
        )
        return doctor.check_whatsapp_gif_decoder(config, probe=False)

    def test_present_is_ok(self, tmp_path, monkeypatch):
        assert self._check(tmp_path, monkeypatch, found=True).status == "ok"

    def test_absent_warns_and_says_what_a_gif_gets(self, tmp_path, monkeypatch):
        result = self._check(tmp_path, monkeypatch, found=False)

        assert result.status == "warn"
        assert "whisper" in result.remedy

    def test_cloud_has_no_gif_to_decode(self, tmp_path, monkeypatch):
        result = self._check(
            tmp_path, monkeypatch, found=False, provider="whatsapp_cloud",
        )

        assert result.status == "skip"
        assert "GIF" in result.detail
