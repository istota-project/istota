"""Normalization and the two avatar stores (`src/istota/avatars.py`).

Stage 1 of the profile-icons spec. Everything here runs against real Pillow and
a real SQLite file: `normalize` is an untrusted-image decode path, so a test
that mocked the decoder would assert nothing about the thing being guarded.
"""

from __future__ import annotations

import io
import sqlite3
import zlib

import pytest
from PIL import Image, ImageFile, JpegImagePlugin

from istota import avatars, user_profiles
from istota.avatars import AvatarError


# --- image fixtures ---------------------------------------------------------


def _png(size=(500, 300), color=(120, 130, 140), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def _jpeg(size=(500, 300), color=(120, 130, 140), *, exif=None, icc=None) -> bytes:
    buf = io.BytesIO()
    kwargs: dict = {}
    if exif is not None:
        kwargs["exif"] = exif
    if icc is not None:
        kwargs["icc_profile"] = icc
    Image.new("RGB", size, color).save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def _png_declaring(width: int, height: int) -> bytes:
    """A PNG whose IHDR *claims* `width`x`height` but carries 1x1 of pixel data.

    The pixel ceiling is a check on the declared dimensions, so the test image
    has to declare them without containing them — a real 4000x4000 PNG would
    pass through a decode-first implementation just as happily, only slower.
    Pillow verifies the IHDR CRC, so it is recomputed over the patched chunk.
    """
    base = _png(size=(1, 1))
    length = int.from_bytes(base[8:12], "big")
    chunk_type = base[12:16]
    assert chunk_type == b"IHDR"
    data = bytearray(base[16:16 + length])
    data[0:4] = width.to_bytes(4, "big")
    data[4:8] = height.to_bytes(4, "big")
    chunk = chunk_type + bytes(data)
    crc = zlib.crc32(chunk) & 0xFFFFFFFF
    return base[:8] + base[8:12] + chunk + crc.to_bytes(4, "big") + base[16 + length + 4:]


def _open(webp: bytes) -> Image.Image:
    return Image.open(io.BytesIO(webp))


BIG = 10 * 1024 * 1024


# --- normalization ----------------------------------------------------------


class TestNormalizeRoundTrip:
    def test_png_becomes_a_192_square_webp(self):
        out, digest = avatars.normalize(_png(), declared_format=None, max_bytes=BIG)
        img = _open(out)
        assert img.format == "WEBP"
        assert img.size == (avatars.AVATAR_EDGE, avatars.AVATAR_EDGE)
        assert len(digest) == 64

    def test_hash_is_stable_across_two_calls(self):
        raw = _png()
        first = avatars.normalize(raw, declared_format=None, max_bytes=BIG)
        second = avatars.normalize(raw, declared_format=None, max_bytes=BIG)
        assert first == second

    def test_hash_is_of_the_normalized_bytes(self):
        import hashlib
        out, digest = avatars.normalize(_png(), declared_format=None, max_bytes=BIG)
        assert digest == hashlib.sha256(out).hexdigest()

    def test_declared_format_does_not_decide_acceptance(self):
        # A browser sends application/octet-stream for a dragged file; the
        # format Pillow reports is the authority.
        out, _ = avatars.normalize(
            _png(), declared_format="application/octet-stream", max_bytes=BIG
        )
        assert _open(out).size == (192, 192)


class TestNormalizeCrop:
    def test_the_crop_is_centred_not_letterboxed(self):
        # 500x300 with a red block dead centre and a green stripe down the far
        # left. A centre crop keeps the red and loses the green; a letterbox
        # keeps both.
        img = Image.new("RGB", (500, 300), (255, 255, 255))
        for x in range(230, 270):
            for y in range(130, 170):
                img.putpixel((x, y), (255, 0, 0))
        for x in range(0, 10):
            for y in range(300):
                img.putpixel((x, y), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, "PNG")

        out, _ = avatars.normalize(buf.getvalue(), declared_format=None, max_bytes=BIG)
        result = _open(out).convert("RGB")

        r, g, b = result.getpixel((96, 96))
        assert r > 150 and g < 110 and b < 110, f"centre mark lost: {(r, g, b)}"
        pixels = list(result.convert("RGB").tobytes())
        triples = list(zip(pixels[0::3], pixels[1::3], pixels[2::3]))
        greens = [px for px in triples if px[1] > 150 and px[0] < 110 and px[2] < 110]
        assert not greens, "the left edge survived, so this was a letterbox not a crop"

    def test_a_portrait_image_comes_out_square(self):
        out, _ = avatars.normalize(_png(size=(200, 900)), declared_format=None, max_bytes=BIG)
        assert _open(out).size == (192, 192)


class TestNormalizeOrientation:
    @staticmethod
    def _two_tone_jpeg(orientation: int | None) -> bytes:
        img = Image.new("RGB", (400, 400))
        for x in range(400):
            colour = (220, 20, 20) if x < 200 else (20, 20, 220)
            for y in range(400):
                img.putpixel((x, y), colour)
        exif = None
        if orientation is not None:
            tags = Image.Exif()
            tags[0x0112] = orientation
            exif = tags.tobytes()
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95, **({"exif": exif} if exif else {}))
        return buf.getvalue()

    def test_orientation_6_lands_upright(self):
        # Stored left-red / right-blue; orientation 6 means "rotate 90 CW to
        # display", so the corrected image is top-red / bottom-blue. Asserted on
        # pixels, because an implementation that merely dropped the EXIF tag
        # would pass an assertion about the tag's absence.
        out, _ = avatars.normalize(
            self._two_tone_jpeg(6), declared_format=None, max_bytes=BIG
        )
        result = _open(out).convert("RGB")
        top = result.getpixel((96, 20))
        bottom = result.getpixel((96, 172))
        assert top[0] > top[2] + 60, f"top should be red, got {top}"
        assert bottom[2] > bottom[0] + 60, f"bottom should be blue, got {bottom}"

    def test_control_no_orientation_tag_stays_left_right(self):
        out, _ = avatars.normalize(
            self._two_tone_jpeg(None), declared_format=None, max_bytes=BIG
        )
        result = _open(out).convert("RGB")
        left = result.getpixel((20, 96))
        right = result.getpixel((172, 96))
        assert left[0] > left[2] + 60, f"left should be red, got {left}"
        assert right[2] > right[0] + 60, f"right should be blue, got {right}"


class TestNormalizeStrips:
    def test_output_carries_no_exif_and_no_icc_profile(self):
        tags = Image.Exif()
        tags[0x0112] = 1
        tags[0x010F] = "ACME Camera Co"
        raw = _jpeg(exif=tags.tobytes(), icc=b"\x00\x01not-a-real-icc-profile")

        # Control: the input really does carry both, so the assertion below is
        # about the pipeline rather than about an input that never had them.
        source = Image.open(io.BytesIO(raw))
        assert source.info.get("icc_profile")
        assert dict(source.getexif())

        out, _ = avatars.normalize(raw, declared_format=None, max_bytes=BIG)
        result = _open(out)
        assert result.info.get("icc_profile") is None
        assert result.info.get("exif") is None
        assert dict(result.getexif()) == {}


class TestNormalizeRefusals:
    @pytest.mark.parametrize("raw, label", [
        (b"", "empty"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>', "svg"),
        (b"<!doctype html><html><body>not an image</body></html>", "html"),
        (b"\x00\x01\x02\x03\x04\x05\x06\x07", "garbage"),
    ])
    def test_non_raster_input_is_415(self, raw, label):
        with pytest.raises(AvatarError) as excinfo:
            avatars.normalize(raw, declared_format=None, max_bytes=BIG)
        assert excinfo.value.status == 415, label

    def test_truncated_jpeg_is_415_not_a_pillow_exception(self):
        raw = _jpeg(size=(900, 900))
        with pytest.raises(AvatarError) as excinfo:
            avatars.normalize(raw[: len(raw) // 2], declared_format=None, max_bytes=BIG)
        assert excinfo.value.status == 415

    def test_one_byte_over_the_cap_is_413(self):
        raw = _png()
        with pytest.raises(AvatarError) as excinfo:
            avatars.normalize(raw, declared_format=None, max_bytes=len(raw) - 1)
        assert excinfo.value.status == 413

    def test_exactly_at_the_cap_is_accepted(self):
        raw = _png()
        out, _ = avatars.normalize(raw, declared_format=None, max_bytes=len(raw))
        assert _open(out).size == (192, 192)

    def test_declared_pixel_count_over_the_ceiling_is_413_before_any_decode(self, monkeypatch):
        # Spy rather than patch-to-raise: `exif_transpose` calls `load` on the
        # happy path, so a module-level patch that failed loudly would turn
        # every other test in this file red.
        #
        # Both classes are patched. `ImageFile.load` overrides `Image.load`, and
        # what `Image.open` returns is a `PngImageFile`, so a spy on the base
        # class alone records nothing and the test passes against an
        # implementation that decodes first.
        raw = _png_declaring(4000, 4000)  # built before the spy is installed

        calls: list[object] = []
        for klass in (Image.Image, ImageFile.ImageFile):
            real_load = klass.load

            def spy(self, *args, _real=real_load, **kwargs):
                calls.append(self)
                return _real(self, *args, **kwargs)

            monkeypatch.setattr(klass, "load", spy)

        with pytest.raises(AvatarError) as excinfo:
            avatars.normalize(raw, declared_format=None, max_bytes=BIG)
        assert excinfo.value.status == 413
        assert calls == [], "the image was decoded before the ceiling was checked"

    def test_a_format_outside_the_accept_list_is_415(self):
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (10, 20, 30)).save(buf, "BMP")
        with pytest.raises(AvatarError) as excinfo:
            avatars.normalize(buf.getvalue(), declared_format="image/bmp", max_bytes=BIG)
        assert excinfo.value.status == 415

    def test_accepted_formats_holds_pillow_format_names_not_mime_types(self):
        assert not any("/" in name for name in avatars.ACCEPTED_FORMATS)

    def test_the_picker_attribute_is_narrower_than_image_star(self):
        assert "image/*" not in avatars.ACCEPT_ATTRIBUTE
        assert "image/svg" not in avatars.ACCEPT_ATTRIBUTE


class TestNormalizeModes:
    def test_rgba_input_yields_an_opaque_output(self):
        img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        out, _ = avatars.normalize(buf.getvalue(), declared_format=None, max_bytes=BIG)
        result = _open(out)
        assert result.mode == "RGB"
        # A fully transparent upload flattens onto white, which is what was sent.
        assert result.convert("RGB").getpixel((96, 96)) == (255, 255, 255)

    def test_animated_gif_yields_one_frame(self):
        frames = [Image.new("RGB", (300, 300), c) for c in [(255, 0, 0), (0, 255, 0)]]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
        assert Image.open(io.BytesIO(buf.getvalue())).n_frames == 2

        out, _ = avatars.normalize(buf.getvalue(), declared_format=None, max_bytes=BIG)
        result = _open(out)
        assert getattr(result, "n_frames", 1) == 1
        assert not getattr(result, "is_animated", False)

    def test_greyscale_input_is_accepted(self):
        out, _ = avatars.normalize(
            _png(size=(400, 400), color=90, mode="L"), declared_format=None, max_bytes=BIG
        )
        assert _open(out).size == (192, 192)


class TestNormalizeDraft:
    def test_a_large_jpeg_is_downsampled_at_decode_time(self, monkeypatch):
        # Without `draft` a large photograph fully decodes into RAM before being
        # thumbnailed, on a host that instruments its own memory pressure.
        # `JpegImageFile` overrides `draft`, so the spy goes on the subclass —
        # on `Image.Image` it records nothing and passes against an
        # implementation that never calls it.
        calls = []
        real_draft = JpegImagePlugin.JpegImageFile.draft

        def spy(self, mode, size):
            calls.append((mode, size))
            return real_draft(self, mode, size)

        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
        avatars.normalize(_jpeg(size=(1600, 1200)), declared_format=None, max_bytes=BIG)
        assert calls, "draft() was never called on a JPEG well over the target edge"
        assert calls[0][1] == (avatars.AVATAR_EDGE, avatars.AVATAR_EDGE)


# --- the user avatar store --------------------------------------------------


def _store(conn, user_id, source, colour=(10, 20, 30)):
    image, digest = avatars.normalize(
        _png(size=(400, 400), color=colour), declared_format=None, max_bytes=BIG
    )
    avatars.put_user_avatar(
        conn, user_id, source=source, image=image, content_hash=digest
    )
    return image, digest


class TestUserAvatarStore:
    def test_round_trip(self, db_conn):
        image, digest = _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        got = avatars.get_user_avatar(db_conn, "alice")
        assert got is not None
        assert got.user_id == "alice"
        assert got.source == avatars.SOURCE_UPLOAD
        assert got.mime == avatars.NORMALIZED_MIME
        assert got.content_hash == digest
        assert got.image == image
        assert got.updated_at

    def test_missing_user_reads_as_none(self, db_conn):
        assert avatars.get_user_avatar(db_conn, "ghost") is None
        assert avatars.user_avatar_hash(db_conn, "ghost") is None

    def test_two_sources_coexist_and_upload_wins(self, db_conn):
        _, nc_hash = _store(db_conn, "alice", avatars.SOURCE_NEXTCLOUD, (200, 10, 10))
        _, up_hash = _store(db_conn, "alice", avatars.SOURCE_UPLOAD, (10, 200, 10))
        assert up_hash != nc_hash

        rows = db_conn.execute(
            "SELECT source FROM user_avatars WHERE user_id = ?", ("alice",)
        ).fetchall()
        assert {r[0] for r in rows} == {avatars.SOURCE_UPLOAD, avatars.SOURCE_NEXTCLOUD}

        got = avatars.get_user_avatar(db_conn, "alice")
        assert got.source == avatars.SOURCE_UPLOAD
        assert avatars.user_avatar_hash(db_conn, "alice") == up_hash

    def test_deleting_the_upload_reveals_the_import(self, db_conn):
        _, nc_hash = _store(db_conn, "alice", avatars.SOURCE_NEXTCLOUD, (200, 10, 10))
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD, (10, 200, 10))

        assert avatars.delete_user_avatar(db_conn, "alice", avatars.SOURCE_UPLOAD) is True
        got = avatars.get_user_avatar(db_conn, "alice")
        assert got.source == avatars.SOURCE_NEXTCLOUD
        assert avatars.user_avatar_hash(db_conn, "alice") == nc_hash

    def test_deleting_what_is_not_there_reads_as_false(self, db_conn):
        assert avatars.delete_user_avatar(db_conn, "alice", avatars.SOURCE_UPLOAD) is False

    def test_put_replaces_the_row_for_that_source(self, db_conn):
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD, (200, 10, 10))
        _, second = _store(db_conn, "alice", avatars.SOURCE_UPLOAD, (10, 200, 10))
        rows = db_conn.execute(
            "SELECT COUNT(*) FROM user_avatars WHERE user_id = ?", ("alice",)
        ).fetchone()
        assert rows[0] == 1
        assert avatars.user_avatar_hash(db_conn, "alice") == second

    def test_hash_lookup_does_not_select_the_blob(self, db_conn):
        # `/me` and the room list call this per render; selecting the blob here
        # would pull ~10 KB out of SQLite for a 64-character answer. Read off
        # the statements SQLite actually saw, not off the module source.
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        seen: list[str] = []
        db_conn.set_trace_callback(seen.append)
        try:
            avatars.user_avatar_hash(db_conn, "alice")
        finally:
            db_conn.set_trace_callback(None)
        assert seen, "no statement was executed"
        for statement in seen:
            select = statement.lower().split("from")[0]
            assert "image" not in select and "*" not in select, statement

    def test_users_do_not_see_each_others_rows(self, db_conn):
        _, alice_hash = _store(db_conn, "alice", avatars.SOURCE_UPLOAD, (200, 10, 10))
        _store(db_conn, "bob", avatars.SOURCE_UPLOAD, (10, 200, 10))
        assert avatars.user_avatar_hash(db_conn, "alice") == alice_hash
        assert avatars.get_user_avatar(db_conn, "bob").content_hash != alice_hash

    def test_delete_all_returns_the_row_count(self, db_conn):
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        _store(db_conn, "alice", avatars.SOURCE_NEXTCLOUD)
        _store(db_conn, "bob", avatars.SOURCE_UPLOAD)
        assert avatars.delete_all_user_avatars(db_conn, "alice") == 2
        assert avatars.get_user_avatar(db_conn, "alice") is None
        assert avatars.get_user_avatar(db_conn, "bob") is not None
        assert avatars.delete_all_user_avatars(db_conn, "alice") == 0


class TestImportProbe:
    def test_a_probe_row_is_not_an_avatar(self, db_conn):
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"abc"')
        assert avatars.get_user_avatar(db_conn, "alice") is None
        assert avatars.user_avatar_hash(db_conn, "alice") is None
        # The row exists — the point is that it is not readable as an avatar.
        row = db_conn.execute(
            "SELECT image, remote_etag FROM user_avatars WHERE user_id = ? AND source = ?",
            ("alice", avatars.SOURCE_NEXTCLOUD),
        ).fetchone()
        assert row is not None
        assert row["image"] is None
        assert row["remote_etag"] == 'W/"abc"'

    def test_a_probe_row_does_not_mask_an_upload(self, db_conn):
        _, up_hash = _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"abc"')
        assert avatars.user_avatar_hash(db_conn, "alice") == up_hash

    def test_import_probe_state_includes_probe_rows(self, db_conn):
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"alice"')
        _store(db_conn, "bob", avatars.SOURCE_NEXTCLOUD)
        avatars.put_user_avatar(
            db_conn, "carol", source=avatars.SOURCE_NEXTCLOUD,
            image=b"", content_hash="x", remote_etag='W/"carol"',
        )
        state = avatars.import_probe_state(db_conn)
        assert state["alice"] == 'W/"alice"'
        assert state["bob"] == ""
        assert state["carol"] == 'W/"carol"'

    def test_import_probe_state_ignores_uploads(self, db_conn):
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        assert avatars.import_probe_state(db_conn) == {}

    def test_touching_a_probe_twice_updates_the_etag_and_keeps_one_row(self, db_conn):
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"one"')
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"two"')
        rows = db_conn.execute(
            "SELECT remote_etag FROM user_avatars WHERE user_id = ?", ("alice",)
        ).fetchall()
        assert [r[0] for r in rows] == ['W/"two"']

    def test_touching_a_probe_never_clears_a_stored_image(self, db_conn):
        # A user who removes their Nextcloud avatar keeps the imported copy: the
        # probe records the new ETag and nothing overwrites the bytes.
        image, digest = _store(db_conn, "alice", avatars.SOURCE_NEXTCLOUD)
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"gone"')
        got = avatars.get_user_avatar(db_conn, "alice")
        assert got is not None
        assert got.image == image
        assert got.content_hash == digest
        assert avatars.import_probe_state(db_conn)["alice"] == 'W/"gone"'


# --- the bot avatar store ---------------------------------------------------


class TestBotAvatarStore:
    def test_empty_reads_as_none(self, db_conn):
        assert avatars.get_bot_avatar(db_conn) is None
        assert avatars.bot_avatar_hash(db_conn) is None
        assert avatars.delete_bot_avatar(db_conn) is False

    def test_round_trip(self, db_conn):
        image, digest = avatars.normalize(
            _png(size=(400, 400)), declared_format=None, max_bytes=BIG
        )
        avatars.put_bot_avatar(db_conn, image=image, content_hash=digest)
        got = avatars.get_bot_avatar(db_conn)
        assert got is not None
        assert got.image == image
        assert got.content_hash == digest
        assert got.mime == avatars.NORMALIZED_MIME
        assert got.updated_at
        assert avatars.bot_avatar_hash(db_conn) == digest

    def test_a_second_write_replaces_the_one_row(self, db_conn):
        first, first_hash = avatars.normalize(
            _png(size=(400, 400), color=(200, 10, 10)), declared_format=None, max_bytes=BIG
        )
        second, second_hash = avatars.normalize(
            _png(size=(400, 400), color=(10, 200, 10)), declared_format=None, max_bytes=BIG
        )
        assert first != second
        avatars.put_bot_avatar(db_conn, image=first, content_hash=first_hash)
        avatars.put_bot_avatar(db_conn, image=second, content_hash=second_hash)
        assert db_conn.execute("SELECT COUNT(*) FROM bot_avatar").fetchone()[0] == 1
        assert avatars.bot_avatar_hash(db_conn) == second_hash

    def test_delete_is_idempotent(self, db_conn):
        image, digest = avatars.normalize(
            _png(size=(400, 400)), declared_format=None, max_bytes=BIG
        )
        avatars.put_bot_avatar(db_conn, image=image, content_hash=digest)
        assert avatars.delete_bot_avatar(db_conn) is True
        assert avatars.delete_bot_avatar(db_conn) is False
        assert avatars.get_bot_avatar(db_conn) is None

    def test_the_bot_row_is_not_a_user_row(self, db_conn):
        image, digest = avatars.normalize(
            _png(size=(400, 400)), declared_format=None, max_bytes=BIG
        )
        avatars.put_bot_avatar(db_conn, image=image, content_hash=digest)
        assert avatars.get_user_avatar(db_conn, "bot") is None


# --- schema -----------------------------------------------------------------


class TestSchema:
    def test_both_tables_exist_on_a_fresh_install(self, db_path):
        with sqlite3.connect(db_path) as conn:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert {"user_avatars", "bot_avatar"} <= names

    def test_migrations_alone_create_both_tables(self, tmp_path):
        # An existing deployment never re-runs a fresh `schema.sql` against an
        # empty file; `_run_migrations` is what it gets. Both paths have to
        # produce the tables or the upgrade lands a daemon that 500s on /me.
        from istota import db as db_module

        path = tmp_path / "upgraded.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            db_module._run_migrations(conn)
            conn.commit()
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        assert {"user_avatars", "bot_avatar"} <= names

    def test_the_bot_table_holds_at_most_one_row(self, db_conn):
        db_conn.execute(
            "INSERT INTO bot_avatar (id, content_hash, image) VALUES (1, 'a', X'00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO bot_avatar (id, content_hash, image) VALUES (2, 'b', X'00')"
            )


# --- deleting a user takes their avatars with them --------------------------


class TestDeleteProfile:
    def test_deleting_a_profile_removes_every_avatar_row(self, db_path, db_conn):
        user_profiles.ensure_profile(db_path, "alice")
        user_profiles.ensure_profile(db_path, "bob")
        _store(db_conn, "alice", avatars.SOURCE_UPLOAD)
        _store(db_conn, "alice", avatars.SOURCE_NEXTCLOUD)
        _store(db_conn, "bob", avatars.SOURCE_UPLOAD)
        db_conn.commit()

        assert user_profiles.delete_profile(db_path, "alice") is True

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            left = {
                r["user_id"] for r in conn.execute("SELECT user_id FROM user_avatars")
            }
        assert left == {"bob"}

    def test_it_takes_a_probe_row_too(self, db_path, db_conn):
        user_profiles.ensure_profile(db_path, "alice")
        avatars.touch_import_probe(db_conn, "alice", remote_etag='W/"abc"')
        db_conn.commit()

        user_profiles.delete_profile(db_path, "alice")

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM user_avatars").fetchone()[0] == 0

    def test_it_opens_exactly_one_connection(self, db_path, monkeypatch):
        """The avatar delete rides the profile delete's own connection.

        `delete_profile` runs inside an open write transaction, and a *second*
        connection opened there waits out the 30s busy timeout on the lock the
        caller is already holding and then raises — the hazard AGENTS.md
        documents for `notification_store`. Asserted by counting opens rather
        than by timing the stall, because a 30s wall-clock assertion is both
        slow and flaky; the count is the property, and it is what a naive
        `delete_all_user_avatars(db_path, ...)` gets wrong.
        """
        user_profiles.ensure_profile(db_path, "alice")

        real_connect = sqlite3.connect
        opened: list[object] = []

        def counting_connect(*args, **kwargs):
            opened.append(args[0] if args else kwargs.get("database"))
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        user_profiles.delete_profile(db_path, "alice")
        monkeypatch.undo()

        assert len(opened) == 1, f"delete_profile opened {len(opened)} connections"

    def test_deleting_a_missing_profile_still_reads_as_false(self, db_path):
        assert user_profiles.delete_profile(db_path, "ghost") is False
