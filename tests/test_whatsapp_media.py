"""Staging inbound WhatsApp media: the path rule, the naming rule, the sniff.

Two properties carry the boundary and are written first, before the module
they are about exists: a name off the wire is never joined under the staging
root unless it is a single ordinary path component, and a staged write never
follows a symlink planted at the name. Everything else here is about the file
that results — what it is sniffed as, what suffix the inbox copy takes from
that sniff, and what the sweep does with one nobody consumed.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from istota import image_attachments
from istota.config import Config, UserConfig
from istota.transport.whatsapp import media

from .support.drift import source_of

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


_heic_cache: list[bytes] = []


def _heic_bytes() -> bytes:
    """A real HEIC, encoded here rather than committed.

    A committed binary would be an opaque blob in a public repository, and
    producing one needs `pillow-heif` either way — which is a core dependency,
    so it is present in the lean `--extra test` install. **This is not a
    substitute for a device file**: it pins that the brand allowlist matches
    what the encoder in this tree produces, not that it matches what an iPhone
    produces.
    """
    import io

    pillow_heif = pytest.importorskip("pillow_heif", reason="HEIF encoder absent")
    if _heic_cache:
        return _heic_cache[0]
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, format="HEIF")
    _heic_cache.append(buf.getvalue())
    return _heic_cache[0]


def _config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "db" / "istota.db",
        temp_dir=tmp_path / "tmp",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig()},
    )


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestTheNameIsASinglePathComponent:
    """`session_log_read.find_logs`' rule, on a value a sidecar chose.

    The empty string mattering is the point there and it is the point here:
    `PurePath` discards an empty component, so `media_dir / ""` is the staging
    root itself, and `..` is a child by name and the parent on disk.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "",
            ".",
            "..",
            "a/b",
            "/abs",
            "../escape.jpg",
            "sub/dir/file.jpg",
            "trailing/",
            "nul\x00.jpg",
            ".hidden",
            "-leading.jpg",
            "name with spaces.jpg",
            "x" * 200,
        ],
        ids=[
            "empty", "dot", "dotdot", "relative", "absolute", "traversal",
            "nested", "trailing-separator", "nul", "dotfile", "leading-dash",
            "spaces", "too-long",
        ],
    )
    def test_a_name_that_is_not_one_ordinary_component_is_refused(self, value):
        assert media.is_staged_name(value) is False

    @pytest.mark.parametrize(
        "value", [None, 42, b"name.jpg", [], {}],
        ids=["none", "int", "bytes", "list", "dict"],
    )
    def test_a_value_that_is_not_a_string_is_refused(self, value):
        """The caller is a JSON decoder reading a frame off a socket, so the
        type is whatever was on the line rather than whatever the sidecar
        meant."""
        assert media.is_staged_name(value) is False

    def test_an_ordinary_name_is_accepted(self):
        assert media.is_staged_name("a1b2c3d4e5f6-0011223344556677.jpg") is True

    def test_the_names_this_module_mints_pass_its_own_validator(self):
        """The sidecar cannot compute `message_fingerprint` — the salt is the
        daemon's — so the validator is the component test plus a bound rather
        than a match of this format. It still has to admit this format."""
        for ext in ("jpg", "png", "heic", "bin"):
            assert media.is_staged_name(media.staged_name("wamid.001", ext)) is True

    def test_a_refused_name_never_reaches_the_filesystem(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        outside = tmp_path / "outside.jpg"

        with pytest.raises(ValueError):
            media.open_staged_write(staging, "../outside.jpg")

        assert not outside.exists()
        assert list(staging.iterdir()) == []


class TestTheStagedWriteDoesNotFollowASymlink:
    def test_a_symlink_planted_at_the_name_is_refused(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        victim = tmp_path / "victim.txt"
        victim.write_text("do not overwrite me")
        name = media.staged_name("wamid.001", "jpg")
        os.symlink(victim, staging / name)

        with pytest.raises(OSError):
            media.open_staged_write(staging, name)

        assert victim.read_text() == "do not overwrite me"

    def test_an_existing_regular_file_at_the_name_is_refused(self, tmp_path):
        """`O_EXCL`, so a name is claimed once. Two messages never collide on
        one — the random half of the name is what keeps them apart — but a
        name that is somehow already taken must not be written through."""
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        name = media.staged_name("wamid.001", "jpg")
        (staging / name).write_bytes(b"first")

        with pytest.raises(FileExistsError):
            media.open_staged_write(staging, name)

        assert (staging / name).read_bytes() == b"first"

    def test_a_write_lands_at_0600_inside_the_staging_directory(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        name = media.staged_name("wamid.001", "jpg")

        fd = media.open_staged_write(staging, name)
        try:
            os.write(fd, b"\x89PNG\r\n\x1a\n")
        finally:
            os.close(fd)

        written = staging / name
        assert written.read_bytes() == b"\x89PNG\r\n\x1a\n"
        assert Path(written).stat().st_mode & 0o777 == 0o600


class TestTheStagingDirectory:
    def test_the_path_is_beside_the_database_with_no_override(self, tmp_path):
        """`default_session_dir`'s location, and deliberately not its
        configured-path arm: the directory is a property of the surface rather
        than of one adapter, so no override ships."""
        config = _config(tmp_path)
        assert media.default_media_dir(config) == tmp_path / "db" / "whatsapp-media"

    def test_a_fresh_directory_is_private(self, tmp_path):
        path = media.ensure_media_dir(tmp_path / "whatsapp-media")
        assert mode_of(path) == 0o700

    def test_an_existing_wide_directory_is_narrowed(self, tmp_path):
        """The case a `mkdir(mode=…)` cannot cover, and the one that happens:
        the mode argument applies only to a directory the call creates."""
        staging = tmp_path / "whatsapp-media"
        staging.mkdir(mode=0o755)
        os.chmod(staging, 0o755)

        media.ensure_media_dir(staging)

        assert mode_of(staging) == 0o700

    def test_a_symlink_at_the_name_is_refused_rather_than_followed(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        staging = tmp_path / "whatsapp-media"
        staging.symlink_to(elsewhere)

        with pytest.raises(OSError):
            media.ensure_media_dir(staging)

    def test_a_directory_owned_by_another_account_is_refused(self, tmp_path):
        """Private is not the same as ours, and 0700 is the case that hides it:
        the `fchmod` is skipped, and with it the `EPERM` that would otherwise
        be the only sign another uid owns the path."""
        staging = tmp_path / "whatsapp-media"
        staging.mkdir(mode=0o700)
        real_fstat = os.fstat

        def foreign(fd):
            info = real_fstat(fd)
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                 os.geteuid() + 1, info.st_gid, info.st_size,
                 int(info.st_atime), int(info.st_mtime), int(info.st_ctime))
            )

        with mock.patch.object(os, "fstat", foreign):
            with pytest.raises(PermissionError):
                media.ensure_media_dir(staging)

    def test_a_file_at_the_name_is_refused(self, tmp_path):
        staging = tmp_path / "whatsapp-media"
        staging.write_text("not a directory")
        with pytest.raises(NotADirectoryError):
            media.ensure_media_dir(staging)


class TestTheSniffIsTheOnlyTypeAuthority:
    @pytest.mark.parametrize(
        "kind,expected",
        [("png", "image/png"), ("jpeg", "image/jpeg"), ("heic", "image/heic")],
    )
    def test_a_decodable_signature_is_accepted(self, tmp_path, kind, expected):
        payload = {"png": PNG, "jpeg": JPEG}.get(kind) or _heic_bytes()
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        path = staging / media.staged_name("wamid.x", "bin")
        path.write_bytes(payload)

        assert media.sniff_staged(path) == expected

    @pytest.mark.parametrize(
        "payload",
        [SVG, MP4, b"", b"hello there"],
        ids=["svg", "mp4", "empty", "text"],
    )
    def test_anything_else_is_refused(self, tmp_path, payload):
        """The SVG-named-`.png` case, and the MP4 whose `ftyp` box a bare
        container test would have typed as an image."""
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        path = staging / media.staged_name("wamid.x", "png")
        path.write_bytes(payload)

        assert media.sniff_staged(path) is None

    def test_a_missing_file_sniffs_as_nothing_rather_than_raising(self, tmp_path):
        assert media.sniff_staged(tmp_path / "gone.jpg") is None

    def test_it_holds_no_signature_table_of_its_own(self):
        """A second sniffer is exactly the duplication `image_sniff` exists to
        prevent, so the delegation is asserted rather than assumed."""
        text = source_of(media.sniff_staged)
        assert "sniff_decodable" in text
        # The docstring names the formats it is about, so the scan is over the
        # code below it.
        body = text.split('"""')[-1]
        for signature in ("ftyp", "PNG", "RIFF", "GIF8", "xff\\xd8"):
            assert signature not in body


class TestTheInboxCopyTakesItsSuffixFromTheSniff:
    """The staged suffix is advisory; the inbox suffix is load-bearing.

    `prepare_image_attachments` screens by `Path(candidate).suffix`, so a
    correct HEIC landing as `.bin` is skipped downstream in silence — the model
    answers without the image and without knowing one was sent.
    """

    def _stage(self, tmp_path, payload, ext):
        staging = media.ensure_media_dir(media.default_media_dir(_config(tmp_path)))
        name = media.staged_name("wamid.001", ext)
        fd = media.open_staged_write(staging, name)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return staging / name

    def test_a_heic_staged_as_bin_lands_in_the_inbox_as_heic(self, tmp_path):
        config = _config(tmp_path)
        staged = self._stage(tmp_path, _heic_bytes(), "bin")

        attachment = media.stage_to_attachment(config, "alice", staged)

        assert attachment is not None
        assert attachment.endswith(".heic")
        assert not staged.exists()

    def test_a_png_staged_as_heic_lands_in_the_inbox_as_png(self, tmp_path):
        """The other direction, which is the one a declared mimetype gets
        wrong: the sidecar's `media_mime` names the type the sender claimed."""
        config = _config(tmp_path)
        staged = self._stage(tmp_path, PNG, "heic")

        attachment = media.stage_to_attachment(config, "alice", staged)

        assert attachment is not None
        assert attachment.endswith(".png")

    def test_the_copy_lands_in_that_users_inbox(self, tmp_path):
        config = _config(tmp_path)
        staged = self._stage(tmp_path, JPEG, "jpg")

        attachment = media.stage_to_attachment(config, "alice", staged)

        assert attachment.startswith("/Users/alice/inbox/")
        landed = config.workspace_path / attachment.lstrip("/")
        assert landed.read_bytes() == JPEG
        assert landed.name.startswith(media.INBOX_NAME_PREFIX + "_")

    def test_a_file_that_is_not_a_decodable_image_is_unlinked(self, tmp_path):
        config = _config(tmp_path)
        staged = self._stage(tmp_path, SVG, "png")

        assert media.stage_to_attachment(config, "alice", staged) is None
        assert not staged.exists()

    def test_a_failed_upload_falls_back_to_the_local_path(self, tmp_path, monkeypatch):
        """`transport/email/inbound.py`'s shipped behaviour for the same
        situation — and the re-derived suffix goes on the fallback too, since
        it is handed to the same screen."""
        config = _config(tmp_path)
        staged = self._stage(tmp_path, _heic_bytes(), "bin")
        monkeypatch.setattr(
            "istota.storage.upload_file_to_inbox_v2", lambda *a, **k: None,
        )

        attachment = media.stage_to_attachment(config, "alice", staged)

        assert attachment is not None
        assert attachment.endswith(".heic")
        assert Path(attachment).exists()
        assert not staged.exists()

    def test_a_raising_upload_costs_the_media_and_not_the_caller(
        self, tmp_path, monkeypatch
    ):
        """The caller is about to open `BEGIN IMMEDIATE`, and the batch's
        contract is that an exception rolls back to a 503 the provider retries
        against. A media failure must cost the media."""
        config = _config(tmp_path)
        staged = self._stage(tmp_path, PNG, "png")

        def boom(*a, **k):
            raise OSError("the mount went away")

        monkeypatch.setattr("istota.storage.upload_file_to_inbox_v2", boom)

        assert media.stage_to_attachment(config, "alice", staged) is None
        assert not staged.exists()


class TestTheSweep:
    def test_a_file_past_the_window_is_unlinked_and_counted(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        old = staging / media.staged_name("wamid.old", "jpg")
        old.write_bytes(JPEG)
        fresh = staging / media.staged_name("wamid.new", "jpg")
        fresh.write_bytes(JPEG)
        now = old.stat().st_mtime + media.MEDIA_ORPHAN_SECONDS + 1
        os.utime(fresh, (now, now))

        assert media.prune_media_dir(staging, now=now) == 1

        assert not old.exists()
        assert fresh.exists()

    def test_an_empty_or_missing_directory_is_not_a_fault(self, tmp_path):
        """Two processes prune the same directory on the split Ansible shape,
        so a sweep that found nothing is the ordinary case rather than a
        fault."""
        assert media.prune_media_dir(tmp_path / "never-made") == 0
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        assert media.prune_media_dir(staging) == 0

    def test_a_directory_at_the_ceiling_prunes_first_and_then_answers(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        old = staging / media.staged_name("wamid.old", "jpg")
        old.write_bytes(b"x" * 4096)
        now = old.stat().st_mtime + media.MEDIA_ORPHAN_SECONDS + 1

        assert media.has_staging_room(staging, incoming_bytes=4096, now=now) is True
        assert not old.exists()

    def test_a_write_that_would_pass_the_ceiling_is_refused(self, tmp_path):
        staging = media.ensure_media_dir(tmp_path / "whatsapp-media")
        fresh = staging / media.staged_name("wamid.new", "jpg")
        fresh.write_bytes(b"x" * 8192)

        assert media.has_staging_room(
            staging, incoming_bytes=media.MEDIA_STAGING_CEILING_BYTES
        ) is False
        # Age is the only rule the sweep applies: a young file may be
        # mid-consume for another message, so freeing space by taking one
        # would be the sweep racing that consume.
        assert fresh.exists()


class TestTheCaps:
    def test_one_file_may_not_weigh_more_than_the_pipeline_accepts(self):
        """A file staged, copied into somebody's inbox and then refused
        downstream for size is the worst shape available here."""
        assert media.MAX_MEDIA_BYTES <= image_attachments.MAX_SOURCE_BYTES

    def test_the_bounds_are_the_ones_the_spec_names(self):
        assert media.MAX_MEDIA_BYTES == 16 * 1024 * 1024
        assert media.MEDIA_STAGING_CEILING_BYTES == 256 * 1024 * 1024
        assert media.MEDIA_ORPHAN_SECONDS == 600
        assert media.MEDIA_DIR_NAME == "whatsapp-media"
